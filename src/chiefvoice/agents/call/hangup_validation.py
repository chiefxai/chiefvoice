"""Server-side validation and idempotency for call completion and hangup execution."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from chiefvoice.agents.call.audio_diagnostics import CallAudioDiagnostics
from chiefvoice.sessions import AcceptedSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HangupValidationResult:
    allowed: bool
    status: str
    error: str | None = None
    reason: str | None = None
    missing_fields: list[str] | None = None
    message: str | None = None


def extract_mandatory_fields(session: AcceptedSession) -> set[str]:
    """Determine mandatory fields from output schema, session metadata, or live context."""
    mandatory: set[str] = set()

    # 1. Post-call output schema required fields
    if isinstance(session.post_call_output_schema, dict):
        required = session.post_call_output_schema.get("required")
        if isinstance(required, (list, tuple, set)):
            mandatory.update(str(f) for f in required)

    # 2. Metadata-configured mandatory fields
    meta_mandatory = session.metadata.get("mandatory_fields")
    if isinstance(meta_mandatory, (list, tuple, set)):
        mandatory.update(str(f) for f in meta_mandatory)
    elif isinstance(meta_mandatory, str):
        mandatory.add(meta_mandatory)

    # 3. Live context mandatory fields if explicitly declared
    if isinstance(session.live_agent_context, dict):
        ctx_mandatory = session.live_agent_context.get("mandatory_fields")
        if isinstance(ctx_mandatory, (list, tuple, set)):
            mandatory.update(str(f) for f in ctx_mandatory)

    return mandatory


def validate_hangup_request(
    session: AcceptedSession,
    arguments: dict[str, Any] | None = None,
    *,
    diagnostics: CallAudioDiagnostics | None = None,
) -> HangupValidationResult:
    """Validate server-side whether a hangup request from Gemini is permitted.

    Verifies:
    1. Idempotency (if already terminating or terminated, returns idempotent success).
    2. Mandatory information completeness (cannot end call if required fields are missing).
    3. Premature termination check (caller turns >= 1 unless caller declined/requested).
    4. Pluggable custom completion validation callback if configured on session metadata.
    """
    args = arguments if isinstance(arguments, dict) else {}
    reason = str(args.get("reason", "completed")).strip().lower()

    # 1. Check Idempotency
    if session.metadata.get("call_terminating") or session.metadata.get("call_terminated"):
        if diagnostics is not None:
            diagnostics.record_conversation_event("hangup_idempotent_duplicate")
        logger.info(
            "call_agent_hangup_idempotent_duplicate",
            extra={"session_id": session.session_id, "provider": session.provider},
        )
        return HangupValidationResult(
            allowed=True,
            status="already_terminating",
            message="Call termination is already in progress.",
        )

    # Early exit reasons where caller refuses or cannot provide details:
    early_exit_reasons = {
        "caller_declined",
        "caller_requested",
        "caller_hung_up",
        "cannot_provide",
        "inactivity",
        "emergency",
        "wrong_number",
    }
    is_early_exit = any(early_reason in reason for early_reason in early_exit_reasons)

    # 2. Check premature interaction (did caller speak at least once?)
    caller_turns = session.metadata.get("caller_turn_count", 0)
    if caller_turns == 0 and not is_early_exit:
        if diagnostics is not None:
            diagnostics.record_conversation_event("hangup_premature_rejected")
            diagnostics.record_conversation_event("hangup_validation_rejected")
        logger.warning(
            "call_agent_hangup_rejected_no_interaction",
            extra={"session_id": session.session_id, "provider": session.provider},
        )
        return HangupValidationResult(
            allowed=False,
            status="rejected",
            error="premature_hangup",
            reason="no_caller_interaction",
            message=(
                "Cannot end call yet: no caller response has been received. "
                "Please wait for the caller to respond before ending the call."
            ),
        )

    # 3. Check mandatory information if this is a completion exit
    mandatory_fields = extract_mandatory_fields(session)
    if mandatory_fields and not is_early_exit:
        collected: dict[str, Any] = {}
        # Merge existing session-collected data
        if isinstance(session.metadata.get("collected_data"), dict):
            collected.update(session.metadata["collected_data"])
        # Merge tool call collected data
        tool_collected = args.get("collected_data")
        if isinstance(tool_collected, dict):
            collected.update(tool_collected)
        # Also check top-level arguments
        for k, v in args.items():
            if k not in {"reason", "summary", "notes", "collected_data"}:
                collected[k] = v

        missing = [
            field
            for field in sorted(mandatory_fields)
            if field not in collected or collected[field] is None or collected[field] == ""
        ]
        if missing:
            if diagnostics is not None:
                diagnostics.record_conversation_event("hangup_premature_rejected")
                diagnostics.record_conversation_event("hangup_validation_rejected")
            logger.warning(
                "call_agent_hangup_rejected_missing_mandatory_fields",
                extra={
                    "session_id": session.session_id,
                    "missing_fields": missing,
                    "collected_fields": list(collected.keys()),
                },
            )
            return HangupValidationResult(
                allowed=False,
                status="rejected",
                error="premature_hangup",
                reason="mandatory_fields_missing",
                missing_fields=missing,
                message=(
                    f"Cannot end call yet: mandatory information has not been collected. "
                    f"Missing fields: {', '.join(missing)}. "
                    "Please ask the caller for this information before ending the call."
                ),
            )

        # Update verified collected data back into metadata
        session.metadata.setdefault("collected_data", {}).update(collected)

    # 4. Custom completion validator callback
    validator_cb = session.metadata.get("conversation_completion_validator")
    if callable(validator_cb):
        try:
            valid, err_msg = validator_cb(session, args)
            if not valid:
                if diagnostics is not None:
                    diagnostics.record_conversation_event("hangup_validation_rejected")
                logger.warning(
                    "call_agent_hangup_rejected_by_custom_validator",
                    extra={"session_id": session.session_id, "error": err_msg},
                )
                return HangupValidationResult(
                    allowed=False,
                    status="rejected",
                    error="validation_failed",
                    message=err_msg or "Conversation completion validation failed.",
                )
        except Exception as exc:
            logger.exception("call_agent_hangup_custom_validator_error", extra={"error": str(exc)})

    # Validation passed
    if diagnostics is not None:
        diagnostics.record_conversation_event("hangup_validation_passed")
    session.metadata["call_terminating"] = True
    return HangupValidationResult(
        allowed=True,
        status="approved",
        message="Conversation completion validated successfully.",
    )
