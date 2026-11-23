from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    OutputAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager

from chiefvoice.agents.call.audio_diagnostics import CallAudioDiagnostics
from chiefvoice.agents.call.conversation_reliability import GeminiConversationWatchdog


class FakeService:
    def __init__(self, *, ready: bool = True):
        self._session = object() if ready else None
        self._ready_for_realtime_input = ready
        self._bot_is_responding = False
        self.recoveries = 0
        self.hangups = 0
        self.recovery_started = asyncio.Event()
        self.recovery_gate = None
        self.recovery_error = None

    async def recover_unanswered_turn(self):
        self.recoveries += 1
        self.recovery_started.set()
        if self.recovery_gate is not None:
            await self.recovery_gate.wait()
        if self.recovery_error is not None:
            raise self.recovery_error
        return bool(self._session and self._ready_for_realtime_input)

    async def play_fallback_and_hangup(self):
        self.hangups += 1
        return True


class FakeWatchdog(GeminiConversationWatchdog):
    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        return None


async def make_watchdog(
    service=None,
    *,
    response_timeout=0.02,
    inactivity_timeout=0.02,
    recovery_grace=0,
):
    session = SimpleNamespace(
        session_id="watchdog-test",
        provider="vobiz",
        auto_hangup_when_complete=False,
        metadata={},
    )
    diagnostics = CallAudioDiagnostics(session)

    watchdog = FakeWatchdog(
        session,
        service or FakeService(),
        diagnostics,
        response_timeout_seconds=response_timeout,
        recovery_grace_seconds=recovery_grace,
        caller_inactivity_seconds=inactivity_timeout,
    )

    setup = FrameProcessorSetup(
        clock=SystemClock(),
        task_manager=TaskManager(),
        pipeline_worker=SimpleNamespace(),
    )
    await watchdog.setup(setup)

    return watchdog, diagnostics


@pytest.mark.asyncio
async def test_watchdog_does_not_arm_until_caller_turn_is_complete():
    watchdog, _ = await make_watchdog(response_timeout=10)
    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)
    assert watchdog._watch_task is None

    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert watchdog._watch_task is not None
    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_caller_speech_cancels_pending_response_watchdog():
    watchdog, _ = await make_watchdog(response_timeout=10)
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    task = watchdog._watch_task
    assert task is not None

    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)
    assert task.cancelled()
    assert watchdog._watch_task is None
    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_gemini_response_start_cancels_watchdog():
    watchdog, _ = await make_watchdog(response_timeout=10)
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    task = watchdog._watch_task
    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)
    assert task is not None and task.cancelled()
    assert watchdog._watch_task is None
    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_stale_turn_cannot_recover_after_new_caller_turn():
    service = FakeService()
    watchdog, _ = await make_watchdog(
        service, response_timeout=0.01, recovery_grace=0
    )
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    old_task = watchdog._watch_task
    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.03)
    assert old_task is not None
    assert old_task.cancelled()
    assert service.recoveries == 1  # only the current turn can recover
    assert service.hangups == 1  # unrecovered silence follows the fallback contract
    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_unready_gemini_waits_for_recovery_decision_then_falls_back_once():
    service = FakeService(ready=False)
    service.recovery_gate = asyncio.Event()
    watchdog, _ = await make_watchdog(service, response_timeout=0.001)
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.wait_for(service.recovery_started.wait(), timeout=1)
    task = watchdog._watch_task
    assert task is not None
    # Do not hang up while the reconnect/readiness decision is still pending.
    assert service.hangups == 0
    service.recovery_gate.set()
    await task
    assert service.recoveries == 1
    assert service.hangups == 1
    assert watchdog._watch_task is None
    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_readiness_restored_during_recovery_leads_to_one_fallback():
    service = FakeService(ready=False)
    service.recovery_gate = asyncio.Event()
    watchdog, _ = await make_watchdog(service, response_timeout=0.001)
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.wait_for(service.recovery_started.wait(), timeout=1)
    task = watchdog._watch_task
    assert task is not None
    service._session = object()
    service._ready_for_realtime_input = True
    service.recovery_gate.set()
    await task

    assert service.recoveries == 1
    assert service.hangups == 1
    assert watchdog._watch_task is None
    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_recovery_exception_falls_back_once():
    service = FakeService()
    service.recovery_error = RuntimeError("reconnect failed")
    watchdog, _ = await make_watchdog(service, response_timeout=0.001)
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    task = watchdog._watch_task
    assert task is not None
    await task

    assert service.recoveries == 1
    assert service.hangups == 1
    assert watchdog._watch_task is None
    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_cleanup_cancels_watchdog_recovery_task():
    service = FakeService(ready=False)
    service.recovery_gate = asyncio.Event()
    watchdog, _ = await make_watchdog(service, response_timeout=0.001)
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.wait_for(service.recovery_started.wait(), timeout=1)
    task = watchdog._watch_task
    assert task is not None and not task.done()

    await watchdog.cleanup()

    assert task.cancelled()
    assert service.hangups == 0


@pytest.mark.asyncio
async def test_caller_inactivity_is_separate_and_never_recovers_or_hangs_up():
    service = FakeService()
    watchdog, diagnostics = await make_watchdog(
        service, response_timeout=10, inactivity_timeout=0.001
    )
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.02)
    assert diagnostics._vad_events["caller_inactivity_timeout"] == 1
    # Caller inactivity must never trigger Gemini recovery.
    assert service.recoveries == 0
    assert service.hangups == 0
    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_initial_llm_run_arms_only_one_initial_greeting_watchdog():
    watchdog, _ = await make_watchdog(response_timeout=10)
    await watchdog.process_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)
    task = watchdog._watch_task
    await watchdog.process_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)
    assert watchdog._watch_task is task
    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_llm_response_end_clears_generation_state():
    watchdog, _ = await make_watchdog(response_timeout=10)

    await watchdog.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    assert watchdog._gemini_is_generating is True
    await watchdog.process_frame(
        OutputAudioRawFrame(audio=b"hello", sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )

    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    assert watchdog._gemini_is_generating is False
    assert watchdog._caller_inactivity_task is not None

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_downstream_interruption_clears_generation_state():
    watchdog, _ = await make_watchdog(response_timeout=10)

    await watchdog.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    assert watchdog._gemini_is_generating is True

    await watchdog.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    assert watchdog._gemini_is_generating is False

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_playback_stop_does_not_mark_generation_complete():
    watchdog, _ = await make_watchdog(response_timeout=10)

    await watchdog.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(
        OutputAudioRawFrame(audio=b"hello", sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert watchdog._bot_audio_playing is False
    assert watchdog._gemini_is_generating is True
    assert watchdog._caller_inactivity_task is None

    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    assert watchdog._gemini_is_generating is False
    assert watchdog._caller_inactivity_task is not None

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_inactivity_timer_waits_for_playback_to_finish():
    watchdog, _ = await make_watchdog(response_timeout=10)

    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(
        OutputAudioRawFrame(audio=b"hello", sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    assert watchdog._gemini_is_generating is False
    assert watchdog._bot_audio_playing is True
    assert watchdog._caller_inactivity_task is None

    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

    assert watchdog._bot_audio_playing is False
    assert watchdog._caller_inactivity_task is not None

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_active_bot_playback_prevents_response_watchdog_arming():
    service = FakeService()
    watchdog, _ = await make_watchdog(service, response_timeout=10)

    watchdog._bot_audio_playing = True

    watchdog._arm_response_watchdog("test_active_playback")

    assert watchdog._watch_task is None
    assert service.recoveries == 0
    assert service.hangups == 0

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_llm_response_start_cancels_pending_response_watchdog():
    watchdog, _ = await make_watchdog(response_timeout=10)

    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    task = watchdog._watch_task
    assert task is not None

    await watchdog.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0)

    assert task.cancelled()
    generation_task = watchdog._watch_task
    assert generation_task is not None
    assert generation_task is not task
    assert "gemini_generation_watchdog" in generation_task.get_name()
    assert watchdog._gemini_is_generating is True

    await watchdog.cleanup()
