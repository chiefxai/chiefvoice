"""Attendance-specific live identity capture capability."""

from __future__ import annotations

import logging
import math
from typing import Any

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

from chiefvoice.agents.call.session_state import collected_data_lock, initialize_collected_data
from chiefvoice.agents.call.providers.live_extensions import LiveCallToolExtension
from chiefvoice.sessions import AcceptedSession

logger = logging.getLogger(__name__)
IDENTITY_FIELDS = frozenset({"caregiver_identity", "patient_identity"})
IDENTITY_CONFIDENCE_THRESHOLD = 0.8


def required_identity_fields(session: AcceptedSession) -> set[str]:
    required: set[str] = set()
    sources = [session.metadata.get("mandatory_fields")]
    if isinstance(session.live_agent_context, dict):
        sources.append(session.live_agent_context.get("mandatory_fields"))
    for source in sources:
        if isinstance(source, str):
            required.add(source)
        elif isinstance(source, (list, tuple, set)):
            required.update(item for item in source if isinstance(item, str))
    contract = session.live_agent_completion_requirements
    if isinstance(contract, dict):
        required.update(item for item in contract.get("always", []) if isinstance(item, str))
        for branch in contract.get("when", []):
            if isinstance(branch, dict):
                field = branch.get("field")
                if isinstance(field, str):
                    required.add(field)
                required.update(item for item in branch.get("required", []) if isinstance(item, str))
    return required & IDENTITY_FIELDS


def _valid_value(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


async def save_caller_identity(
    session: AcceptedSession,
    *,
    identity_type: str,
    name: str,
    confidence: float,
    explicitly_stated: bool,
) -> dict[str, Any]:
    field = identity_type if identity_type in IDENTITY_FIELDS else None
    if (
        field is None or not isinstance(name, str) or not name.strip() or len(name.strip()) > 200
        or not explicitly_stated or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float)) or not math.isfinite(confidence)
        or not IDENTITY_CONFIDENCE_THRESHOLD <= confidence <= 1.0
    ):
        collected = initialize_collected_data(session)
        return {
            "status": "uncertain", "identity_field": field,
            "known_identity_fields": sorted(key for key in IDENTITY_FIELDS if _valid_value(collected.get(key))),
            "message": "No identity was saved. Clarify only if this field is still required.",
        }

    normalized = name.strip()
    async with collected_data_lock(session):
        collected = initialize_collected_data(session)
        previous = collected.get(field)
        if isinstance(previous, str) and previous.strip().casefold() == normalized.casefold():
            status = "reused"
        else:
            collected[field] = normalized
            status = "saved" if not _valid_value(previous) else "updated"
        known = sorted(key for key in IDENTITY_FIELDS if _valid_value(collected.get(key)))
        missing = sorted(key for key in required_identity_fields(session) if not _valid_value(collected.get(key)))
    logger.info("attendance_identity_captured", extra={"session_id": session.session_id, "identity_field": field, "status": status})
    return {
        "status": status, "identity_field": field, "known_identity_fields": known,
        "missing_required_identity_fields": missing,
        "message": "This identity is saved. Do not ask for it again. Ask only for a listed missing identity, if any; otherwise continue the call.",
    }


class AttendanceLiveIdentityExtension(LiveCallToolExtension):
    """Adds attendance's identity tool only to explicitly selected calls."""

    def configure(self, session: AcceptedSession) -> None:
        session.metadata.setdefault("workflow_protected_fields", set()).update(IDENTITY_FIELDS)
        session.metadata.setdefault("workflow_string_fields", set()).update(IDENTITY_FIELDS)

    def register(self, session: AcceptedSession, capture: Any):
        self.configure(session)

        async def record_identity(params: FunctionCallParams) -> None:
            arguments = params.arguments if isinstance(params.arguments, dict) else {}
            capture(session, tool_name="record_caller_identity", arguments=arguments)
            confidence = arguments.get("confidence")
            try:
                result = await save_caller_identity(
                    session,
                    identity_type=arguments.get("identity_type", "") if isinstance(arguments.get("identity_type"), str) else "",
                    name=arguments.get("name", "") if isinstance(arguments.get("name"), str) else "",
                    confidence=confidence if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else 0.0,
                    explicitly_stated=arguments.get("explicitly_stated") is True,
                )
                await params.result_callback(result)
            except Exception as exc:
                logger.warning("attendance_identity_capture_failed", extra={"session_id": session.session_id, "error_type": type(exc).__name__})
                await params.result_callback({"status": "error", "message": "Identity could not be saved."})

        tool = FunctionSchema(
            name="record_caller_identity",
            description="Save a caller-provided caregiver or patient name when the caller explicitly states the role and name. Preserve the original script and do not guess.",
            properties={
                "identity_type": {"type": "string", "enum": ["caregiver_identity", "patient_identity"]},
                "name": {"type": "string"},
                "explicitly_stated": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": IDENTITY_CONFIDENCE_THRESHOLD, "maximum": 1},
            },
            required=["identity_type", "name", "explicitly_stated", "confidence"],
            handler=record_identity,
        )
        required = sorted(required_identity_fields(session))
        instructions = ""
        if required:
            instructions = (
                "Attendance identity collection is available through record_caller_identity. "
                "When the caller explicitly gives a caregiver or patient name, capture its role, "
                "preserve the original script, and do not guess or copy one role into another. "
                f"Required identity fields: {', '.join(required)}."
            )
        return [tool], instructions
