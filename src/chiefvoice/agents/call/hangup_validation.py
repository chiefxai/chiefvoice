"""Server-side validation and idempotency for call completion and hangup execution."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from chiefvoice.agents.call.audio_diagnostics import CallAudioDiagnostics
from chiefvoice.agents.call.identity_state import IDENTITY_FIELDS, collected_data_lock
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
    """Return fields explicitly required before ending the live conversation.

    Post-call output schemas describe the extraction result, not information that
    must be collected before hang-up. They are validated by the post-call worker
    and must never prevent the live agent from completing a call.
    """
    mandatory: set[str] = set()

    # Only explicit live-call requirements can gate hang-up.
    meta_mandatory = session.metadata.get("mandatory_fields")
    if isinstance(meta_mandatory, (list, tuple, set)):
        mandatory.update(str(f) for f in meta_mandatory)
    elif isinstance(meta_mandatory, str):
        mandatory.add(meta_mandatory)

    # Live context may explicitly declare required collected fields.
    if isinstance(session.live_agent_context, dict):
        ctx_mandatory = session.live_agent_context.get("mandatory_fields")
        if isinstance(ctx_mandatory, (list, tuple, set)):
            mandatory.update(str(f) for f in ctx_mandatory)

    return mandatory


def _required_completion_fields(
    session: AcceptedSession, collected: dict[str, Any]
) -> tuple[set[str], set[str]]:
    """Resolve required fields and reject undeclared conditional outcomes.

    A conditional contract is also an allow-list for completed outcomes. Without
    this check, an agent could report an unlisted value (for example ``unclear``)
    and satisfy only the unconditional fields, ending the call without collecting
    the branch-specific information.
    """
    requirements = session.live_agent_completion_requirements
    if not isinstance(requirements, dict):
        return set(), set()
    required = set(requirements.get("always", []))
    conditional_values: dict[str, list[Any]] = {}
    for branch in requirements.get("when", []):
        field = branch.get("field")
        expected = branch.get("equals")
        actual = collected.get(field)
        if isinstance(field, str) and field:
            required.add(field)
            conditional_values.setdefault(field, []).append(expected)
        # JSON booleans and numbers compare equal in Python (True == 1), but
        # they are distinct values in a caller's completion contract.
        if type(actual) is type(expected) and actual == expected:
            required.update(branch.get("required", []))

    unmatched = {
        field
        for field, allowed_values in conditional_values.items()
        if field not in collected
        or not any(
            type(collected[field]) is type(expected) and collected[field] == expected
            for expected in allowed_values
        )
    }
    return required, unmatched


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
    caller_interaction_seen = bool(session.metadata.get("caller_interaction_seen"))
    if caller_turns == 0 and not caller_interaction_seen and not is_early_exit:
        if diagnostics is not None:
            diagnostics.record_conversation_event("hangup_premature_rejected")
            diagnostics.record_conversation_event("hangup_validation_rejected")
        logger.warning(
            "call_agent_hangup_rejected_no_interaction",
            extra={
                "session_id": session.session_id,
                "provider": session.provider,
                "caller_turn_count": caller_turns,
                "caller_interaction_seen": caller_interaction_seen,
            },
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
    completion_requirements = session.live_agent_completion_requirements
    collected: dict[str, Any] = {}
    if isinstance(session.metadata.get("collected_data"), dict):
        collected.update(session.metadata["collected_data"])
    tool_collected = args.get("collected_data")
    if isinstance(tool_collected, dict):
        _merge_collected_values(collected, tool_collected)
    for k, v in args.items():
        if k not in {"reason", "summary", "notes", "collected_data"}:
            _merge_collected_values(collected, {k: v})
    if collected:
        state = session.metadata.get("collected_data")
        if not isinstance(state, dict):
            state = {}
            session.metadata["collected_data"] = state
        state.update(collected)

    if (mandatory_fields or completion_requirements) and not is_early_exit:
        completion_fields, unmatched_outcomes = _required_completion_fields(session, collected)
        required_fields = mandatory_fields | completion_fields
        missing = [
            field
            for field in sorted(required_fields)
            if not _is_valid_field_value(field, collected.get(field))
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
                    f"Hangup was rejected. The only missing required field(s) are: {', '.join(missing)}. "
                    "Do not repeat the closing, thank-you, or goodbye. Ask one short, direct question "
                    "to obtain or confirm only these missing item(s). If the caller already stated "
                    "the information, use that answer without asking again. Then retry end_call once, "
                    "including every required value in collected_data. If the caller cannot or will "
                    "not provide it, use the appropriate early-exit reason."
                ),
            )

        if unmatched_outcomes:
            if diagnostics is not None:
                diagnostics.record_conversation_event("hangup_premature_rejected")
                diagnostics.record_conversation_event("hangup_validation_rejected")
            logger.warning(
                "call_agent_hangup_rejected_unmatched_completion_outcome",
                extra={
                    "session_id": session.session_id,
                    "unmatched_fields": sorted(unmatched_outcomes),
                },
            )
            return HangupValidationResult(
                allowed=False,
                status="rejected",
                error="premature_hangup",
                reason="completion_outcome_unmatched",
                missing_fields=sorted(unmatched_outcomes),
                message=(
                    "Cannot complete the call because the reported outcome does not match "
                    "a configured completion branch. Clarify the caller's answer or use an "
                    "appropriate early-exit reason."
                ),
            )

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
    logger.info(
        "call_agent_hangup_validation_passed",
        extra={
            "session_id": session.session_id,
            "provider": session.provider,
            "collected_fields": sorted(session.metadata.get("collected_data", {}).keys())
            if isinstance(session.metadata.get("collected_data"), dict)
            else [],
        },
    )
    session.metadata["call_terminating"] = True
    return HangupValidationResult(
        allowed=True,
        status="approved",
        message="Conversation completion validated successfully.",
    )


def _is_nonempty(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _is_valid_field_value(field: str, value: Any) -> bool:
    if field in IDENTITY_FIELDS:
        return isinstance(value, str) and bool(value.strip())
    return _is_nonempty(value)


def _merge_collected_values(target: dict[str, Any], values: dict[str, Any]) -> None:
    for key, value in values.items():
        if not _is_valid_field_value(key, value):
            continue
        # End-call arguments are model-generated and have no extraction
        # confidence marker. A saved identity may only be changed through the
        # explicit, confidence-gated identity-capture tool.
        if key in IDENTITY_FIELDS and _is_valid_field_value(key, target.get(key)):
            continue
        target[key] = value


async def validate_hangup_request_async(
    session: AcceptedSession,
    arguments: dict[str, Any] | None = None,
    *,
    diagnostics: CallAudioDiagnostics | None = None,
) -> HangupValidationResult:
    """Serialize validation with live extraction against the shared session data."""
    async with collected_data_lock(session):
        return validate_hangup_request(session, arguments, diagnostics=diagnostics)
