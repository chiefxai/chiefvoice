"""Attendance post-call workflow integration."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from chiefvoice.agents.post_call.base import CompletedCall, PostCallResult
from chiefvoice.agents.post_call.workflows.attendance.validation import (
    expected_attendance_assignment,
    failed_attendance_validation,
    validate_attendance,
)


class _AttendanceEvidence(BaseModel):
    """Proposed field evidence; the validator checks each citation independently."""

    extracted_data: dict[str, Any] = Field(default_factory=dict)
    transcript_status: Literal["clear", "unclear", "no_data"] = "unclear"


class AttendanceWorkflow:
    """Attendance rules isolated from the shared post-call agent."""

    workflow_id = "attendance"

    def __init__(self, chat_model: Any):
        self._chat_model = chat_model

    def compatibility_fields(self, workflow_result: dict[str, Any]) -> dict[str, Any]:
        """Expose the established Athulya result alongside the generic envelope."""
        if workflow_result.get("workflow_id") != self.workflow_id:
            return {}
        if workflow_result.get("status") == "completed":
            data = workflow_result.get("data")
            return {"attendance_validation": data} if isinstance(data, dict) else {}
        if workflow_result.get("status") == "failed":
            return {"attendance_validation": failed_attendance_validation()}
        return {}

    def legacy_manual_review_fields(self, _result: PostCallResult) -> dict[str, Any]:
        """Flag recognized legacy attendance jobs without authoritative workflow config."""
        validation = failed_attendance_validation()
        validation["validation_errors"] = ["workflow_identity_unverified"]
        validation["outcome"] = "unclear"
        return {"attendance_validation": validation}

    def recover_legacy_compatibility_fields(
        self, result: PostCallResult, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Recognize only the historical attendance job payload signature."""
        if (
            "workflow_id" in payload
            or "workflow_context" in payload
            or not isinstance(payload.get("expected_assignment"), dict)
        ):
            return {}
        return self.legacy_manual_review_fields(result)

    async def process(self, call: CompletedCall, result: PostCallResult) -> dict[str, Any]:
        if result.transcript_status == "no_data" or not (call.transcript or "").strip():
            validation = validate_attendance(
                extracted={},
                transcript=None,
                expected=expected_attendance_assignment(call.workflow_context),
                live_claim=call.end_call_claim,
                tool_events=call.tool_events,
                extraction_status="no_data",
            )
            return validation

        model = self._chat_model.with_structured_output(_AttendanceEvidence)
        evidence = await model.ainvoke(
            [
                (
                    "system",
                    "Extract attendance evidence only. Return each field as an object with value, "
                    "source='caller_transcript', original_utterance copied verbatim from one Caller "
                    "turn, and confidence from 0 to 1. Fields: caregiver_identity, patient_identity, "
                    "patient_address, attendance_status (arrived/not_arrived/departed), "
                    "attendance_time, caregiver_confirmation (boolean). Do not cite Assistant speech, "
                    "workflow context, or end-call/tool claims as caller evidence. Preserve the "
                    "original language. Use null/missing when unsupported. Support Tamil, English, "
                    "Malayalam and Telugu; do not treat non-English speech as unintelligible only "
                    "because it is non-English.",
                ),
                (
                    "human",
                    f"Transcript:\n{call.transcript or ''}\n"
                    "Workflow context for comparison only, never caller evidence: "
                    f"{call.workflow_context or {}}\n"
                    f"Captured tool events (claims): {call.tool_events}\n"
                    f"End-call claim (claim only): {call.end_call_claim}",
                ),
            ],
            config={
                "run_name": "attendance_workflow_evidence_extraction",
                "metadata": {
                    "session_id": call.session_id,
                    "tenant_id": call.tenant_id,
                    "workflow_id": self.workflow_id,
                },
            },
        )
        if isinstance(evidence, BaseModel):
            evidence = evidence.model_dump()
        if not isinstance(evidence, dict):
            evidence = {}
        return validate_attendance(
            extracted=evidence.get("extracted_data", {}),
            transcript=call.transcript,
            expected=expected_attendance_assignment(call.workflow_context),
            live_claim=call.end_call_claim,
            tool_events=call.tool_events,
            extraction_status=evidence.get("transcript_status", "unclear"),
        )
