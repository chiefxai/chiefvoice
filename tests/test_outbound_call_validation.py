"""Tests for Phase 3 – Real outbound call validation.

Covers:
9. Perform real outbound call validation
   - Test normal check-in conversations.
   - Test normal check-out conversations.
   - Test long caller pauses.
   - Test delayed Gemini responses.
   - Verify recordings, transcripts and attendance data.
   - Monitor call termination logs.
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager

from chiefvoice.agents.call.audio_diagnostics import CallAudioDiagnostics
from chiefvoice.agents.call.conversation_reliability import (
    CallLifecycleState,
    GeminiConversationWatchdog,
)
from chiefvoice.agents.call.hangup_validation import validate_hangup_request
from chiefvoice.agents.post_call.transcript import TranscriptCollector
from chiefvoice.calls.coordinator import CallCoordinator
from chiefvoice.recordings.storage import EncryptedLocalRecordingStore
from chiefvoice.sessions.models import AcceptedSession, Direction

CHECK_IN_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "employee_id": {"type": "string"},
        "attendance_type": {"type": "string", "enum": ["check_in", "check_out"]},
        "status": {"type": "string", "enum": ["confirmed", "late", "absent"]},
        "timestamp": {"type": "string"},
        "location": {"type": "string"},
    },
    "required": ["employee_id", "attendance_type", "status", "timestamp"],
}

CHECK_OUT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "employee_id": {"type": "string"},
        "attendance_type": {"type": "string", "enum": ["check_in", "check_out"]},
        "status": {"type": "string", "enum": ["confirmed", "early", "overtime"]},
        "timestamp": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["employee_id", "attendance_type", "status", "timestamp"],
}


class SimulatedGeminiService:
    def __init__(self):
        self._session = object()
        self._ready_for_realtime_input = True
        self.recoveries = 0
        self.inactivity_prompts: list[str] = []

    async def recover_unanswered_turn(self) -> bool:
        self.recoveries += 1
        return True

    async def prompt_caller_inactivity(self, text: str) -> bool:
        self.inactivity_prompts.append(text)
        return True


class SimulatedWatchdog(GeminiConversationWatchdog):
    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        return None


async def build_outbound_test_session(
    schema: dict,
    *,
    caller: str = "+15551112222",
    callee: str = "+15553334444",
    tenant_id: str = "tenant-attendance-01",
    first_inactivity: float = 0.03,
    second_inactivity: float = 0.03,
    final_inactivity: float = 0.03,
) -> tuple[AcceptedSession, SimulatedWatchdog, SimulatedGeminiService, CallAudioDiagnostics]:
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        caller=caller,
        callee=callee,
        provider_call_id="vobiz-call-xyz",
        tenant_id=tenant_id,
        post_call_output_schema=schema,
        auto_hangup_when_complete=True,
    )
    diagnostics = CallAudioDiagnostics(session)
    session.metadata["call_audio_diagnostics"] = diagnostics
    svc = SimulatedGeminiService()
    watchdog = SimulatedWatchdog(
        session,
        svc,
        diagnostics,
        response_timeout_seconds=0.03,
        caller_inactivity_first_seconds=first_inactivity,
        caller_inactivity_second_seconds=second_inactivity,
        caller_inactivity_final_seconds=final_inactivity,
    )
    setup = FrameProcessorSetup(
        clock=SystemClock(),
        task_manager=TaskManager(),
        pipeline_worker=SimpleNamespace(),
    )
    await watchdog.setup(setup)
    return session, watchdog, svc, diagnostics


# ==============================================================================
# 9. Real Outbound Call Validation Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_normal_check_in_conversation_validation(tmp_path: Path, caplog):
    """Validate a complete check-in conversation:

    - Transcripts captured from both sides.
    - Mandatory check-in attendance fields collected and verified.
    - Closing audio delivered.
    - Recordings encrypted and saved.
    - Call termination logs emitted.
    """
    caplog.set_level(logging.INFO)
    session, watchdog, svc, diagnostics = await build_outbound_test_session(
        CHECK_IN_OUTPUT_SCHEMA
    )

    # Attach transcript collectors
    caller_tap, assistant_tap = TranscriptCollector.for_pipeline(session)
    caller_tap.push_frame = AsyncMock()
    assistant_tap.push_frame = AsyncMock()

    # Attach recording store
    fernet_key = Fernet.generate_key().decode("ascii")
    recording_store = EncryptedLocalRecordingStore(tmp_path, fernet_key)

    async def finalize_recording():
        uri = await recording_store.save_wav(
            session.tenant_id, session.session_id, b"\x00\x00" * 8000, 8000, 1
        )
        session.metadata["recording_uri"] = uri

    session.metadata["finalize_recording"] = finalize_recording
    session.metadata["recording_pipeline_finished"] = True

    hangups = []

    async def mock_hangup():
        hangups.append(True)
        session.metadata["call_terminating"] = True
        session.metadata["call_terminated"] = True

    session.metadata["hang_up_call"] = mock_hangup

    # 1. Bot greeting
    await assistant_tap.process_frame(
        TTSTextFrame("Good morning! Please provide your employee ID to check in.", "sentences"),
        FrameDirection.DOWNSTREAM,
    )
    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    # 2. Caller responds with employee ID
    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await caller_tap.process_frame(
        TranscriptionFrame("My employee ID is EMP-101 and I am checking in.", "caller", "t1"),
        FrameDirection.UPSTREAM,
    )
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    # 3. Bot confirms and asks shift status
    await assistant_tap.process_frame(
        TTSTextFrame("Thank you. Are you on schedule for the Gate 4 morning shift?", "sentences"),
        FrameDirection.DOWNSTREAM,
    )
    await watchdog.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    # 4. Caller confirms status
    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await caller_tap.process_frame(
        TranscriptionFrame("Yes, confirmed on schedule.", "caller", "t2"),
        FrameDirection.UPSTREAM,
    )
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    # 5. Bot confirms check-in and ends call
    await assistant_tap.process_frame(
        TTSTextFrame("Check-in confirmed for EMP-101. Have a great shift, goodbye!", "sentences"),
        FrameDirection.DOWNSTREAM,
    )
    await watchdog.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    # 6. Gemini invokes hang_up_call with collected attendance data
    collected_attendance = {
        "employee_id": "EMP-101",
        "attendance_type": "check_in",
        "status": "confirmed",
        "timestamp": "2026-11-16T09:00:00Z",
        "location": "Gate 4",
    }
    validation = validate_hangup_request(
        session,
        {"reason": "completed", "collected_data": collected_attendance},
        diagnostics=diagnostics,
    )
    assert validation.allowed is True
    assert validation.status == "approved"

    # Audio finishes playing out
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    # Execute hangup callback
    callback = session.metadata.get("hang_up_call")
    if callable(callback):
        await callback()

    assert len(hangups) == 1

    # End session via coordinator
    coordinator = CallCoordinator({}, AsyncMock())
    await coordinator.end_session(session, "agent_completed")

    # Verify transcripts
    transcript = session.metadata.get("post_call_transcript", "")
    assert "Caller: My employee ID is EMP-101" in transcript
    assert "Assistant: Check-in confirmed for EMP-101" in transcript

    # Verify attendance data
    assert session.metadata["collected_data"] == collected_attendance
    assert session.metadata["collected_data"]["status"] == "confirmed"

    # Verify recording
    assert session.metadata.get("recording_uri") is not None
    rec_file = Path(session.metadata["recording_uri"])
    assert rec_file.exists()
    decrypted = Fernet(fernet_key).decrypt(rec_file.read_bytes())
    with wave.open(io.BytesIO(decrypted), "rb") as wav:
        assert wav.getframerate() == 8000
        assert wav.getnchannels() == 1

    # Verify termination logs
    diagnostics.log_summary()
    log_text = caplog.text
    assert "caller_turn_completed" in log_text
    assert "call_audio_flow_summary" in log_text

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_normal_check_out_conversation_validation(tmp_path: Path, caplog):
    """Validate a complete check-out conversation:

    - Collects check-out timestamp, employee ID, and notes.
    - Verified against check-out output schema.
    - Idempotent termination.
    """
    caplog.set_level(logging.INFO)
    session, watchdog, svc, diagnostics = await build_outbound_test_session(
        CHECK_OUT_OUTPUT_SCHEMA
    )

    caller_tap, assistant_tap = TranscriptCollector.for_pipeline(session)
    caller_tap.push_frame = AsyncMock()
    assistant_tap.push_frame = AsyncMock()

    hangups = []

    async def on_hangup():
        hangups.append(True)

    session.metadata["hang_up_call"] = on_hangup

    # 1. Greeting
    await assistant_tap.process_frame(
        TTSTextFrame(
            "Good evening! Checking out for the day? Please provide your ID.",
            "sentences",
        ),
        FrameDirection.DOWNSTREAM,
    )
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    # 2. Caller response
    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await caller_tap.process_frame(
        TranscriptionFrame(
            "Yes, employee ID EMP-202 checking out. All tasks completed.",
            "caller",
            "t1",
        ),
        FrameDirection.UPSTREAM,
    )
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    # 3. Check-out validation
    collected_attendance = {
        "employee_id": "EMP-202",
        "attendance_type": "check_out",
        "status": "confirmed",
        "timestamp": "2026-11-16T17:30:00Z",
        "notes": "All tasks completed.",
    }
    validation = validate_hangup_request(
        session,
        {"reason": "completed", "collected_data": collected_attendance},
        diagnostics=diagnostics,
    )
    assert validation.allowed is True

    # 4. Closing turn
    await assistant_tap.process_frame(
        TTSTextFrame("Thank you EMP-202. Check-out completed. Goodbye!", "sentences"),
        FrameDirection.DOWNSTREAM,
    )
    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    await session.metadata["hang_up_call"]()
    assert len(hangups) == 1

    # Verify duplicate hangup safety
    duplicate_val = validate_hangup_request(
        session,
        {"reason": "completed", "collected_data": collected_attendance},
        diagnostics=diagnostics,
    )
    assert duplicate_val.status == "already_terminating"

    # Verify transcript and attendance
    assert "EMP-202 checking out" in session.metadata["post_call_transcript"]
    assert session.metadata["collected_data"]["attendance_type"] == "check_out"

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_check_in_with_long_caller_pauses_recovers_and_completes():
    """Test a check-in conversation where caller takes long pauses:

    - Follow-up prompt asks if caller is still there.
    - Caller answers after follow-up.
    - Check-in finishes successfully without premature disconnection.
    """
    session, watchdog, svc, diagnostics = await build_outbound_test_session(
        CHECK_IN_OUTPUT_SCHEMA,
        first_inactivity=0.015,
        second_inactivity=0.015,
    )

    # 1. Greeting
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    # 2. Caller pauses for 25ms (exceeds first_inactivity timer)
    await asyncio.sleep(0.025)

    # Silence manager triggered first follow-up prompt
    assert len(svc.inactivity_prompts) == 1
    assert diagnostics._vad_events["caller_inactivity_first_prompt"] == 1
    assert watchdog._inactivity_stage == 1

    # Bot speaks follow-up prompt
    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    # Caller resumes speech after follow-up
    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert watchdog._inactivity_stage == 0  # Timers reset!

    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert session.metadata["caller_turn_count"] == 1

    # Complete check-in
    val = validate_hangup_request(
        session,
        {
            "reason": "completed",
            "collected_data": {
                "employee_id": "EMP-303",
                "attendance_type": "check_in",
                "status": "confirmed",
                "timestamp": "2026-11-16T09:05:00Z",
            },
        },
        diagnostics=diagnostics,
    )
    assert val.allowed is True

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_check_in_with_delayed_gemini_response_recovers_seamlessly():
    """Test a check-in call where Gemini experiences delayed response:

    - Watchdog triggers recovery turn.
    - Gemini recovers and finishes check-in without dropped call.
    """
    session, watchdog, svc, diagnostics = await build_outbound_test_session(
        CHECK_IN_OUTPUT_SCHEMA
    )

    # Caller provides check-in details
    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    # Gemini delayed past response_timeout (0.03s)
    await asyncio.sleep(0.045)

    # Recovery was triggered
    assert svc.recoveries == 1
    assert diagnostics._vad_events["response_watchdog_fired"] == 1

    # Gemini recovers and sends output
    await watchdog.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(
        OutputAudioRawFrame(audio=b"\x00" * 320, sample_rate=24000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    # Complete check-in
    val = validate_hangup_request(
        session,
        {
            "reason": "completed",
            "collected_data": {
                "employee_id": "EMP-404",
                "attendance_type": "check_in",
                "status": "confirmed",
                "timestamp": "2026-11-16T09:00:00Z",
            },
        },
        diagnostics=diagnostics,
    )
    assert val.allowed is True
    assert watchdog._call_state == CallLifecycleState.ACTIVE

    await watchdog.cleanup()
