"""Tests for Phase 3 – Validation and testing: Watchdog, recovery, and hangup behavior.

Covers:
7. Test watchdog and recovery behaviour
   - Callers pausing for 5–10 seconds.
   - Callers speaking for extended periods.
   - Watchdog cancellation during caller speech.
   - Gemini delayed responses.
   - Verify recovery never terminates an active call.
8. Test hang-up and inactivity behaviour
   - Verify premature Gemini hang-up requests are rejected.
   - Verify completed conversations terminate normally.
   - Test caller resuming after inactivity prompts.
   - Verify duplicate hang-up requests are handled safely.
   - Verify no unexpected call disconnections.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputAudioRawFrame,
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
from chiefvoice.agents.call.hangup_validation import (
    validate_hangup_request,
)
from chiefvoice.sessions.models import AcceptedSession, Direction


class MockGeminiReliableService:
    def __init__(self, *, ready: bool = True, recovery_outcome: bool = True):
        self._session = object() if ready else None
        self._ready_for_realtime_input = ready
        self._bot_is_responding = False
        self.recovery_outcome = recovery_outcome
        self.recoveries = 0
        self.hangups = 0
        self.inactivity_prompts: list[str] = []

    async def recover_unanswered_turn(self) -> bool:
        self.recoveries += 1
        return self.recovery_outcome

    async def prompt_caller_inactivity(self, text: str) -> bool:
        self.inactivity_prompts.append(text)
        return True

    async def play_fallback_and_hangup(self) -> bool:
        self.hangups += 1
        return True


class MockWatchdogProcessor(GeminiConversationWatchdog):
    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        return None


async def setup_test_watchdog(
    session: AcceptedSession | None = None,
    service: MockGeminiReliableService | None = None,
    *,
    response_timeout: float = 0.05,
    recovery_grace: float = 0.01,
    first_inactivity: float = 0.04,
    second_inactivity: float = 0.04,
    final_inactivity: float = 0.04,
) -> tuple[MockWatchdogProcessor, CallAudioDiagnostics, AcceptedSession, MockGeminiReliableService]:
    if session is None:
        session = AcceptedSession(
            channel="phone",
            provider="vobiz",
            direction=Direction.OUTBOUND,
            caller="+15550001111",
            callee="+15552223333",
        )
    diagnostics = CallAudioDiagnostics(session)
    svc = service or MockGeminiReliableService()
    watchdog = MockWatchdogProcessor(
        session,
        svc,
        diagnostics,
        response_timeout_seconds=response_timeout,
        recovery_grace_seconds=recovery_grace,
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
    return watchdog, diagnostics, session, svc


# ==============================================================================
# 7. Test watchdog and recovery behaviour
# ==============================================================================


@pytest.mark.asyncio
async def test_caller_pausing_for_extended_period_does_not_fire_turn_recovery():
    """When a caller pauses for a few seconds, it is silence, NOT a dropped Gemini turn.

    Watchdog recovery must never fire for caller pauses.
    """
    svc = MockGeminiReliableService()
    watchdog, diagnostics, _, _ = await setup_test_watchdog(
        service=svc,
        response_timeout=0.02,
        first_inactivity=0.20,  # Long caller pause
    )

    # Bot finishes speaking greeting
    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    # Caller pauses (silent) for 50ms (longer than response_timeout)
    await asyncio.sleep(0.05)

    # Gemini watchdog must NOT have fired, because it is waiting for the caller, not Gemini
    assert svc.recoveries == 0
    assert diagnostics._vad_events["response_watchdog_fired"] == 0
    assert watchdog._watch_task is None
    assert watchdog._caller_inactivity_task is not None

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_caller_speaking_for_extended_period_keeps_watchdog_unarmed():
    """While caller is speaking for an extended duration, the response watchdog is never armed."""
    svc = MockGeminiReliableService()
    watchdog, diagnostics, _, _ = await setup_test_watchdog(
        service=svc,
        response_timeout=0.02,
    )

    # Caller starts speaking
    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert watchdog._caller_is_speaking is True
    assert watchdog._watch_task is None

    # Caller speaks for an extended period (simulate continuous speech duration)
    for _ in range(5):
        await asyncio.sleep(0.01)
        assert watchdog._watch_task is None
        assert svc.recoveries == 0

    assert diagnostics._vad_events["response_watchdog_fired"] == 0
    assert svc.recoveries == 0

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_watchdog_cancellation_during_caller_speech():
    """If the caller begins speaking again before Gemini responds, the watchdog is cancelled."""
    svc = MockGeminiReliableService()
    watchdog, diagnostics, _, _ = await setup_test_watchdog(
        service=svc,
        response_timeout=0.50,  # Generous response timeout
    )

    # Caller finishes turn 1
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    watch_task = watchdog._watch_task
    assert watch_task is not None
    assert not watch_task.done()

    # Before Gemini responds, caller speaks again
    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    # Watchdog task must be cancelled immediately
    await asyncio.sleep(0)
    assert watch_task.cancelled()
    assert watchdog._watch_task is None
    assert svc.recoveries == 0

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_gemini_delayed_response_triggers_recovery():
    """When Gemini is delayed past response_timeout, the watchdog triggers a recovery turn."""
    svc = MockGeminiReliableService(recovery_outcome=True)
    watchdog, diagnostics, _, _ = await setup_test_watchdog(
        service=svc,
        response_timeout=0.02,
        recovery_grace=0.01,
    )

    # Caller finishes speaking
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert watchdog._watch_task is not None

    # Wait past response_timeout
    await asyncio.sleep(0.04)

    # Recovery must have fired
    assert svc.recoveries == 1
    assert diagnostics._vad_events["response_watchdog_fired"] == 1

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_gemini_recovery_failure_plays_fallback_and_requests_normal_hangup():
    """A silent failed recovery uses the audio-drain fallback hangup path."""
    svc = MockGeminiReliableService(recovery_outcome=False)
    hangups = []

    async def on_hangup():
        hangups.append(True)

    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        metadata={"hang_up_call": on_hangup},
    )
    watchdog, diagnostics, _, _ = await setup_test_watchdog(
        session=session,
        service=svc,
        response_timeout=0.01,
        recovery_grace=0.01,
    )

    # Caller finishes speaking, Gemini fails to respond and recovery fails
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.04)

    assert svc.recoveries == 1
    # The fallback is responsible for delivering its notice before the normal
    # provider hangup path; it must not call the test's direct hangup callback.
    assert len(hangups) == 0
    assert svc.hangups == 1
    assert watchdog._call_state == CallLifecycleState.ACTIVE
    assert not session.metadata.get("call_terminating")
    assert not session.metadata.get("call_terminated")

    await watchdog.cleanup()


# ==============================================================================
# 8. Test hang-up and inactivity behaviour
# ==============================================================================


def test_premature_gemini_hangup_rejected_with_descriptive_error():
    """Premature hang-up without mandatory fields returns detailed rejection message."""
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        live_agent_completion_requirements={
            "always": ["checkout_time", "employee_id"]
        },
        metadata={"caller_turn_count": 1},
    )
    diagnostics = CallAudioDiagnostics(session)

    # Gemini attempts to hang up without collecting employee_id
    result = validate_hangup_request(
        session,
        {"reason": "completed", "collected_data": {"checkout_time": "17:00"}},
        diagnostics=diagnostics,
    )
    assert result.allowed is False
    assert result.error == "premature_hangup"
    assert result.reason == "mandatory_fields_missing"
    assert result.missing_fields == ["employee_id"]
    assert "employee_id" in (result.message or "")


def test_completed_conversation_terminates_normally():
    """When mandatory fields are satisfied, hangup is permitted and recorded."""
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        live_agent_completion_requirements={
            "always": ["checkout_time", "employee_id"]
        },
        metadata={"caller_turn_count": 2},
    )
    diagnostics = CallAudioDiagnostics(session)

    result = validate_hangup_request(
        session,
        {
            "reason": "completed",
            "collected_data": {"checkout_time": "17:00", "employee_id": "EMP-42"},
        },
        diagnostics=diagnostics,
    )
    assert result.allowed is True
    assert result.status == "approved"
    assert session.metadata["collected_data"]["employee_id"] == "EMP-42"


@pytest.mark.asyncio
async def test_caller_resumes_speech_after_first_and_second_inactivity_prompts():
    """Caller can resume at any point during silence prompts, restoring normal conversation."""
    svc = MockGeminiReliableService()
    watchdog, diagnostics, session, _ = await setup_test_watchdog(
        service=svc,
        first_inactivity=0.01,
        second_inactivity=0.01,
        final_inactivity=0.05,
    )

    # Turn 1: Bot stops speaking
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.015)
    assert len(svc.inactivity_prompts) == 1
    assert watchdog._inactivity_stage == 1  # First prompt sent

    # Bot speaks first prompt and stops
    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.015)
    assert len(svc.inactivity_prompts) == 2
    assert watchdog._inactivity_stage == 2  # Second prompt sent

    # Caller resumes speech!
    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert watchdog._inactivity_stage == 0
    assert watchdog._caller_is_speaking is True
    assert watchdog._caller_inactivity_task is None

    # Caller completes their statement
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert session.metadata["caller_turn_count"] == 1

    # Bot responds to caller's statement
    await watchdog.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(
        OutputAudioRawFrame(audio=b"\x00" * 320, sample_rate=24000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    # Inactivity stage was reset, so silence policy restarts from stage 0, NOT terminating!
    assert watchdog._call_state == CallLifecycleState.ACTIVE
    assert watchdog._inactivity_stage == 0

    await watchdog.cleanup()


def test_duplicate_hangup_requests_handled_safely():
    """Repeated hang-up calls are safely recognized as idempotent duplicates."""
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        metadata={"caller_turn_count": 1},
    )
    diagnostics = CallAudioDiagnostics(session)

    # First request
    res1 = validate_hangup_request(session, {"reason": "completed"}, diagnostics=diagnostics)
    assert res1.allowed is True
    session.metadata["call_terminating"] = True

    # Duplicate request
    res2 = validate_hangup_request(session, {"reason": "completed"}, diagnostics=diagnostics)
    assert res2.allowed is True
    assert res2.status == "already_terminating"
    assert diagnostics._vad_events["hangup_idempotent_duplicate"] == 1


@pytest.mark.asyncio
async def test_no_unexpected_call_disconnections():
    """Ensure normal conversational flow with pauses and turns does not disconnect."""
    hangups = []

    async def on_hangup():
        hangups.append(True)

    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        metadata={"hang_up_call": on_hangup},
    )
    svc = MockGeminiReliableService()
    watchdog, _, _, _ = await setup_test_watchdog(
        session=session,
        service=svc,
        response_timeout=0.03,
        first_inactivity=0.03,
    )

    # Simulate 3 turns of active back-and-forth conversation
    for _turn in range(3):
        # Caller speaks
        await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(0.005)
        await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

        # Gemini responds
        await watchdog.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
        await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(0.005)
        await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

        # Natural brief pause between turns
        await asyncio.sleep(0.01)

        # No hangup must have occurred
        assert len(hangups) == 0
        assert watchdog._call_state == CallLifecycleState.ACTIVE

    await watchdog.cleanup()
