"""Tests for Phase 2 – Call termination reliability.

Covers:
4. Secure hang_up_call execution
   - Tool definition and parameters (reason, collected_data, summary).
   - Server-side conversation completion validation.
   - Mandatory information verification.
   - Premature hang-up rejection.
   - Closing audio delivery verification.
   - Idempotent termination.
5. Caller silence management
   - Configurable inactivity timers.
   - First follow-up prompt after silence.
   - Second follow-up prompt if silence continues.
   - Timer and sequence reset when caller speech resumes.
   - Graceful termination after inactivity policy completion.
6. Call-state and asynchronous task management
   - Consistent call and conversation state tracking.
   - Mutual exclusion between watchdog and inactivity tasks.
   - Clean task cancellation on call termination.
   - Race condition prevention between VAD events and hang-up execution.
   - State isolation between concurrent calls.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.services.llm_service import FunctionCallParams
from pipecat.utils.asyncio.task_manager import TaskManager

from chiefvoice.agents.call.audio_diagnostics import CallAudioDiagnostics
from chiefvoice.agents.call.conversation_reliability import (
    CallLifecycleState,
    ConversationTurnState,
    GeminiConversationWatchdog,
)
from chiefvoice.agents.call.hangup_validation import (
    extract_mandatory_fields,
    validate_hangup_request,
)
from chiefvoice.agents.call.providers.vertex import VertexGeminiLiveProvider
from chiefvoice.sessions.models import AcceptedSession, Direction


class FakeReliabilityService:
    def __init__(self, *, ready: bool = True):
        self._session = object() if ready else None
        self._ready_for_realtime_input = ready
        self._bot_is_responding = False
        self.recoveries = 0
        self.inactivity_prompts: list[str] = []

    async def recover_unanswered_turn(self) -> bool:
        self.recoveries += 1
        return bool(self._session and self._ready_for_realtime_input)

    async def prompt_caller_inactivity(self, text: str) -> bool:
        self.inactivity_prompts.append(text)
        return True


class FakeWatchdog(GeminiConversationWatchdog):
    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        return None


async def make_test_watchdog(
    session: AcceptedSession | None = None,
    service: FakeReliabilityService | None = None,
    *,
    response_timeout: float = 0.05,
    first_inactivity: float = 0.02,
    second_inactivity: float = 0.02,
    final_inactivity: float = 0.02,
) -> tuple[FakeWatchdog, CallAudioDiagnostics, AcceptedSession, FakeReliabilityService]:
    if session is None:
        session = AcceptedSession(
            channel="phone",
            provider="vobiz",
            direction=Direction.OUTBOUND,
            caller="+15551234567",
            callee="+15559876543",
        )
    diagnostics = CallAudioDiagnostics(session)
    svc = service or FakeReliabilityService()
    watchdog = FakeWatchdog(
        session,
        svc,
        diagnostics,
        response_timeout_seconds=response_timeout,
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
# 4. Secure hang_up_call execution tests
# ==============================================================================


def test_extract_mandatory_fields():
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        post_call_output_schema={
            "type": "object",
            "required": ["booking_id", "email"],
        },
        metadata={"mandatory_fields": ["phone_verified"]},
    )
    fields = extract_mandatory_fields(session)
    assert fields == {"booking_id", "email", "phone_verified"}


def test_hangup_rejected_when_no_caller_interaction():
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
    )
    diagnostics = CallAudioDiagnostics(session)
    result = validate_hangup_request(
        session, {"reason": "completed"}, diagnostics=diagnostics
    )
    assert result.allowed is False
    assert result.error == "premature_hangup"
    assert result.reason == "no_caller_interaction"
    assert diagnostics._vad_events["hangup_premature_rejected"] == 1


def test_hangup_allowed_after_vad_interaction_without_completed_turn():
    """VAD speech-start counts even if no matching stop-speaking frame arrives."""
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        metadata={"caller_interaction_seen": True},
    )

    result = validate_hangup_request(session, {"reason": "completed"})

    assert result.allowed is True
    assert result.status == "approved"


def test_hangup_allowed_on_caller_declined_without_interaction():
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
    )
    result = validate_hangup_request(session, {"reason": "caller_declined"})
    assert result.allowed is True
    assert result.status == "approved"


def test_hangup_rejected_when_mandatory_information_missing():
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        post_call_output_schema={
            "type": "object",
            "required": ["full_name", "account_number"],
        },
        metadata={"caller_turn_count": 1},
    )
    diagnostics = CallAudioDiagnostics(session)
    # Gemini calls hang_up_call providing only one of the required fields
    result = validate_hangup_request(
        session,
        {"reason": "completed", "collected_data": {"full_name": "Alice"}},
        diagnostics=diagnostics,
    )
    assert result.allowed is False
    assert result.error == "premature_hangup"
    assert result.missing_fields == ["account_number"]
    assert "account_number" in (result.message or "")
    assert diagnostics._vad_events["hangup_premature_rejected"] == 1


def test_hangup_approved_when_all_mandatory_information_collected():
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        post_call_output_schema={
            "type": "object",
            "required": ["full_name", "account_number"],
        },
        metadata={"caller_turn_count": 1},
    )
    diagnostics = CallAudioDiagnostics(session)
    result = validate_hangup_request(
        session,
        {
            "reason": "completed",
            "collected_data": {"full_name": "Alice", "account_number": "ACC-999"},
        },
        diagnostics=diagnostics,
    )
    assert result.allowed is True
    assert result.status == "approved"
    assert diagnostics._vad_events["hangup_validation_passed"] == 1
    # Verify session recorded the collected data
    assert session.metadata["collected_data"]["full_name"] == "Alice"
    assert session.metadata["collected_data"]["account_number"] == "ACC-999"


def test_hangup_idempotency():
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        metadata={"caller_turn_count": 1, "call_terminating": True},
    )
    diagnostics = CallAudioDiagnostics(session)
    result = validate_hangup_request(
        session, {"reason": "completed"}, diagnostics=diagnostics
    )
    assert result.allowed is True
    assert result.status == "already_terminating"
    assert diagnostics._vad_events["hangup_idempotent_duplicate"] == 1


@pytest.mark.asyncio
async def test_vertex_provider_hangup_tool_registration_and_execution(monkeypatch):
    monkeypatch.setattr(
        "pipecat.services.google.gemini_live.vertex.llm.GeminiLiveVertexLLMService._get_credentials",
        lambda *args, **kwargs: MagicMock(),
    )
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        auto_hangup_when_complete=True,
        metadata={"caller_turn_count": 1},
    )
    hangups = []

    async def mock_hangup():
        hangups.append(True)

    session.metadata["hang_up_call"] = mock_hangup

    provider = VertexGeminiLiveProvider(
        project_id="test-proj",
        location="us-central1",
        model="google/gemini-live-2.5-flash-native-audio",
        voice="Charon",
        system_instruction="Test instructions",
    )
    processors = await provider.create_processors(session)
    service = processors[0]

    # Verify tool schema definition
    tools_schema = service._service_tools()
    assert tools_schema is not None
    tool = tools_schema.standard_tools[0]
    assert tool.name == "hang_up_call"
    assert "reason" in tool.properties
    assert "collected_data" in tool.properties
    assert "summary" in tool.properties

    # Execute tool handler
    params = FunctionCallParams(
        function_name="hang_up_call",
        tool_call_id="call-1",
        arguments={"reason": "completed"},
        llm=service,
        pipeline_worker=MagicMock(),
        context=MagicMock(),
        result_callback=AsyncMock(),
    )
    await tool.handler(params)

    assert len(hangups) == 1
    params.result_callback.assert_called_once()
    callback_arg = params.result_callback.call_args[0][0]
    assert callback_arg["success"] is True


@pytest.mark.asyncio
async def test_vertex_provider_hangup_tool_rejects_premature_call(monkeypatch):
    monkeypatch.setattr(
        "pipecat.services.google.gemini_live.vertex.llm.GeminiLiveVertexLLMService._get_credentials",
        lambda *args, **kwargs: MagicMock(),
    )
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        auto_hangup_when_complete=True,
        post_call_output_schema={"required": ["mandatory_info"]},
        metadata={"caller_turn_count": 1},
    )
    hangups = []
    session.metadata["hang_up_call"] = lambda: hangups.append(True)

    provider = VertexGeminiLiveProvider(
        project_id="test-proj",
        location="us-central1",
        model="google/gemini-live-2.5-flash-native-audio",
        voice="Charon",
        system_instruction="Test instructions",
    )
    processors = await provider.create_processors(session)
    service = processors[0]
    tool = service._service_tools().standard_tools[0]

    params = FunctionCallParams(
        function_name="hang_up_call",
        tool_call_id="call-1",
        arguments={"reason": "completed"},
        llm=service,
        pipeline_worker=MagicMock(),
        context=MagicMock(),
        result_callback=AsyncMock(),
    )
    await tool.handler(params)

    # Must be rejected because mandatory_info was not collected
    assert len(hangups) == 0
    params.result_callback.assert_called_once()
    callback_arg = params.result_callback.call_args[0][0]
    assert callback_arg["success"] is False
    assert callback_arg["error"] == "premature_hangup"
    assert callback_arg["missing_fields"] == ["mandatory_info"]


# ==============================================================================
# 5. Implement caller silence management tests
# ==============================================================================


@pytest.mark.asyncio
async def test_caller_silence_first_and_second_followup_prompts():
    service = FakeReliabilityService()
    watchdog, diagnostics, session, _ = await make_test_watchdog(
        service=service,
        first_inactivity=0.01,
        second_inactivity=0.01,
        final_inactivity=0.01,
    )

    # 1. Bot stops speaking -> arms first inactivity stage
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.015)

    # First follow-up prompt should have been triggered
    assert len(service.inactivity_prompts) == 1
    assert diagnostics._vad_events["caller_inactivity_first_prompt"] == 1
    assert watchdog._inactivity_stage == 1

    # Simulate bot playing and finishing the first follow-up prompt
    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.015)

    # Second follow-up prompt should have been triggered
    assert len(service.inactivity_prompts) == 2
    assert diagnostics._vad_events["caller_inactivity_second_prompt"] == 1
    assert watchdog._inactivity_stage == 2

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_caller_speech_resets_silence_timers_and_stage():
    service = FakeReliabilityService()
    watchdog, diagnostics, session, _ = await make_test_watchdog(
        service=service,
        first_inactivity=0.01,
        second_inactivity=0.01,
        final_inactivity=0.01,
    )

    # Bot finishes initial turn -> stage 0 armed
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.015)
    assert watchdog._inactivity_stage == 1
    assert len(service.inactivity_prompts) == 1

    # Caller starts speaking!
    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    # Stage and timer must be completely reset
    assert watchdog._inactivity_stage == 0
    assert watchdog._caller_inactivity_task is None
    assert watchdog._caller_is_speaking is True

    # Caller finishes speaking, bot speaks and finishes
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    # Now the silence sequence restarts from Stage 0 (first prompt), NOT Stage 2!
    await asyncio.sleep(0.015)
    assert watchdog._inactivity_stage == 1
    assert len(service.inactivity_prompts) == 2
    assert diagnostics._vad_events["caller_inactivity_first_prompt"] == 2

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_silence_management_terminates_gracefully_after_policy():
    hangups = []

    async def on_hangup():
        hangups.append(True)

    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        metadata={"hang_up_call": on_hangup},
    )
    service = FakeReliabilityService()
    watchdog, diagnostics, _, _ = await make_test_watchdog(
        session=session,
        service=service,
        first_inactivity=0.01,
        second_inactivity=0.01,
        final_inactivity=0.01,
    )

    # Bot finishes turn -> stage 0 silence
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.015)
    assert watchdog._inactivity_stage == 1  # First prompt sent

    # Bot finishes first prompt -> stage 1 silence
    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.015)
    assert watchdog._inactivity_stage == 2  # Second prompt sent

    # Bot finishes second prompt -> stage 2 final silence
    await watchdog.process_frame(BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.015)

    # Inactivity policy completed -> graceful termination
    assert watchdog._inactivity_stage == 3
    assert diagnostics._vad_events["caller_inactivity_policy_terminated"] == 1
    assert len(hangups) == 1
    assert watchdog._call_state == CallLifecycleState.TERMINATING

    await watchdog.cleanup()


# ==============================================================================
# 6. Call-state and asynchronous task management tests
# ==============================================================================


@pytest.mark.asyncio
async def test_watchdog_and_inactivity_mutual_exclusion():
    watchdog, _, _, _ = await make_test_watchdog(response_timeout=10, first_inactivity=10)

    # 1. Caller finishes speaking -> waiting for bot response
    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert watchdog._turn_state == ConversationTurnState.WAITING_FOR_RESPONSE
    assert watchdog._watch_task is not None
    assert watchdog._caller_inactivity_task is None

    # 2. Bot starts generating -> cancels watchdog
    await watchdog.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    assert watchdog._turn_state == ConversationTurnState.BOT_GENERATING
    assert watchdog._watch_task is None
    assert watchdog._caller_inactivity_task is None

    # 3. Bot finishes generation and playback -> waiting for caller
    await watchdog.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert watchdog._turn_state == ConversationTurnState.AWAITING_CALLER
    assert watchdog._watch_task is None
    assert watchdog._caller_inactivity_task is not None

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_task_cleanup_cancels_all_pending_tasks():
    watchdog, _, session, _ = await make_test_watchdog(response_timeout=10, first_inactivity=10)
    await watchdog.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    task = watchdog._caller_inactivity_task
    assert task is not None and not task.done()

    await watchdog.cleanup()

    assert task.cancelled()
    assert watchdog._call_state == CallLifecycleState.TERMINATED
    assert session.metadata["call_terminated"] is True
    assert watchdog._caller_inactivity_task is None
    assert watchdog._watch_task is None


@pytest.mark.asyncio
async def test_vad_events_ignored_when_call_is_terminating():
    watchdog, _, session, _ = await make_test_watchdog(response_timeout=10, first_inactivity=10)
    session.metadata["call_terminating"] = True

    # Attempt caller speech frames after call is terminating
    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert watchdog._caller_is_speaking is False
    assert watchdog._watch_task is None

    await watchdog.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert watchdog._watch_task is None
    assert watchdog._turn_state != ConversationTurnState.WAITING_FOR_RESPONSE

    await watchdog.cleanup()


@pytest.mark.asyncio
async def test_state_isolation_between_concurrent_calls():
    # Create two independent sessions and watchdogs
    watchdog1, diag1, session1, svc1 = await make_test_watchdog(first_inactivity=0.01)
    watchdog2, diag2, session2, svc2 = await make_test_watchdog(first_inactivity=10.0)

    # Call 1 has silence and triggers first follow-up prompt
    await watchdog1.process_frame(BotStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.015)
    assert len(svc1.inactivity_prompts) == 1
    assert watchdog1._inactivity_stage == 1

    # Call 2 remains unaffected
    assert len(svc2.inactivity_prompts) == 0
    assert watchdog2._inactivity_stage == 0
    assert diag2._vad_events["caller_inactivity_first_prompt"] == 0

    # Call 1 terminates
    await watchdog1.cleanup()
    assert watchdog1._call_state == CallLifecycleState.TERMINATED
    assert watchdog2._call_state == CallLifecycleState.ACTIVE

    await watchdog2.cleanup()
