"""Shared live-call identity state and synchronization helpers."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from chiefvoice.sessions import AcceptedSession

logger = logging.getLogger(__name__)

IDENTITY_FIELDS = frozenset({"caregiver_identity", "patient_identity"})
IDENTITY_CONFIDENCE_THRESHOLD = 0.8


def collected_data_lock(session: AcceptedSession) -> asyncio.Lock:
    """Return the lock shared by live extraction and hangup validation."""
    lock = session.metadata.get("_collected_data_lock")
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        session.metadata["_collected_data_lock"] = lock
    return lock


def initialize_collected_data(session: AcceptedSession) -> dict[str, Any]:
    """Ensure one mutable dictionary for values actually captured during the call.

    Assignment context is an expectation, not caller evidence, so it must never
    initialize caller-collected identity fields.
    """
    collected = session.metadata.get("collected_data")
    if not isinstance(collected, dict):
        collected = {}
        session.metadata["collected_data"] = collected
    return collected


def required_identity_fields(session: AcceptedSession) -> set[str]:
    """Find identity keys required by this call's live completion contract."""
    required: set[str] = set()
    for source in (session.metadata.get("mandatory_fields"),):
        if isinstance(source, str):
            required.add(source)
        elif isinstance(source, (list, tuple, set)):
            required.update(item for item in source if isinstance(item, str))
    if isinstance(session.live_agent_context, dict):
        source = session.live_agent_context.get("mandatory_fields")
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
                required.update(
                    item for item in branch.get("required", []) if isinstance(item, str)
                )
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
    """Persist a confirmed identity without logging its personal value."""
    field = identity_type if identity_type in IDENTITY_FIELDS else None
    logger.info(
        "caller_identity_extraction_attempted",
        extra={
            "session_id": session.session_id,
            "identity_field": field or "invalid",
            "confidence": confidence,
            "explicitly_stated": explicitly_stated,
        },
    )
    if (
        field is None
        or not isinstance(name, str)
        or not name.strip()
        or len(name.strip()) > 200
        or not explicitly_stated
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not IDENTITY_CONFIDENCE_THRESHOLD <= confidence <= 1.0
    ):
        logger.info(
            "caller_identity_extraction_skipped",
            extra={
                "session_id": session.session_id,
                "identity_field": field or "invalid",
                "status": "uncertain_or_invalid",
            },
        )
        collected = initialize_collected_data(session)
        return {
            "status": "uncertain",
            "identity_field": field,
            "known_identity_fields": sorted(
                key for key in IDENTITY_FIELDS if _valid_value(collected.get(key))
            ),
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
        missing_required = sorted(
            key for key in required_identity_fields(session) if not _valid_value(collected.get(key))
        )
    logger.info(
        "caller_identity_session_data_updated",
        extra={
            "session_id": session.session_id,
            "identity_field": field,
            "confidence": confidence,
            "status": status,
            "known_identity_fields": known,
            "missing_required_identity_fields": missing_required,
        },
    )
    return {
        "status": status,
        "identity_field": field,
        "known_identity_fields": known,
        "missing_required_identity_fields": missing_required,
        "message": (
            "This identity is saved. Do not ask for it again. Ask only for a listed missing "
            "identity, if any; otherwise continue the call."
        ),
    }
