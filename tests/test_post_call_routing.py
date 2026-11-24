"""Regression coverage for platform-owned transcript/STT routing decisions."""

from collections import deque
from datetime import UTC, datetime

import pytest

from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.agents.post_call.errors import (
    NoTranscriptDataError,
    PostCallTranscriptionError,
)
from chiefvoice.agents.post_call.langchain_agent import LangChainPostCallAgent
from chiefvoice.agents.post_call.workflows import PostCallWorkflowRegistry
from chiefvoice.calls.coordinator import CallCoordinator
from chiefvoice.calls.routes import _post_call_pipeline_fields
from chiefvoice.sessions import AcceptedSession, Direction

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["checked_out", "unclear"],
        },
        "transcript_summary": {"type": "string"},
    },
    "required": ["outcome", "transcript_summary"],
}


class _FakeStructuredInvoker:
    def __init__(self, model):
        self.model = model

    async def ainvoke(self, messages, config=None):
        self.model.events.append("analysis")
        self.model.messages.append(messages)
        return self.model.responses.popleft()


class _FakeChatModel:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.events = []
        self.messages = []
        self.schemas = []

    def with_structured_output(self, schema):
        self.schemas.append(schema)
        return _FakeStructuredInvoker(self)


def _call(
    *,
    transcript="Caller: I have completed the duty.",
    recording=b"wav",
    language="en-IN",
):
    return CompletedCall(
        session_id="session-1",
        tenant_id="tenant-1",
        provider="vobiz",
        channel="phone",
        direction="outbound",
        live_agent_language=language,
        transcript=transcript,
        recording_audio=recording,
        output_schema=_OUTPUT_SCHEMA,
    )


def _custom_result(*, outcome="checked_out", transcript_status="clear", needs_audio_review=False):
    return {
        "structured_output": {
            "outcome": outcome,
            "transcript_summary": "Caller confirmed duty completion.",
        },
        "transcript_status": transcript_status,
        "needs_audio_review": needs_audio_review,
    }


@pytest.mark.asyncio
async def test_client_unclear_outcome_does_not_trigger_stt_when_platform_transcript_is_clear(
    monkeypatch,
):
    model = _FakeChatModel(
        _custom_result(outcome="unclear", transcript_status="clear")
    )
    class _UnrelatedWorkflow:
        workflow_id = "test.unrelated"

        async def process(self, _call, _result):
            raise AssertionError("no workflow ID was configured for this call")

    registry = PostCallWorkflowRegistry()
    registry.register(_UnrelatedWorkflow())
    agent = LangChainPostCallAgent(model, workflow_registry=registry)
    stt_calls = []

    async def transcribe(_call):
        stt_calls.append(True)
        return "independent transcript"

    monkeypatch.setattr(agent, "_transcribe_recording", transcribe)

    # English is configured for automatic audio review even when the initial
    # transcript assessment is clear. Use an unsupported language to isolate
    # the behavior this test covers: a clear transcript alone must not trigger STT.
    result = await agent.process(_call(language="fr-FR"))

    assert result.structured_output["outcome"] == "unclear"
    assert len(model.messages) == 1
    assert stt_calls == []
    assert result.stt_status == "not_needed"


@pytest.mark.asyncio
async def test_platform_unclear_transcript_triggers_stt_and_second_analysis(monkeypatch):
    model = _FakeChatModel(
        _custom_result(transcript_status="unclear"),
        _custom_result(transcript_status="clear"),
    )
    agent = LangChainPostCallAgent(model)
    events = []

    async def transcribe(_call):
        events.append("stt")
        return "Independent recording transcript."

    monkeypatch.setattr(agent, "_transcribe_recording", transcribe)

    result = await agent.process(_call())

    assert events == ["stt"]
    assert model.events == ["analysis", "analysis"]
    assert result.structured_output["outcome"] == "checked_out"
    assert result.transcript_status == "clear"
    assert result.stt_status == "completed"


@pytest.mark.asyncio
async def test_missing_live_transcript_uses_recording_stt_before_first_analysis(monkeypatch):
    model = _FakeChatModel(_custom_result(transcript_status="clear"))
    agent = LangChainPostCallAgent(model)
    events = []

    async def transcribe(_call):
        events.append("stt")
        return "Transcript recovered from the recording."

    monkeypatch.setattr(agent, "_transcribe_recording", transcribe)

    result = await agent.process(_call(transcript="", recording=b"wav"))

    assert events == ["stt"]
    assert model.events == ["analysis"]
    assert "Transcript recovered from the recording." in str(model.messages[0])
    assert result.structured_output["outcome"] == "checked_out"
    assert result.stt_status == "completed"


@pytest.mark.asyncio
async def test_missing_transcript_and_recording_stops_before_model_analysis():
    model = _FakeChatModel()
    agent = LangChainPostCallAgent(model)

    with pytest.raises(NoTranscriptDataError):
        await agent.process(_call(transcript="", recording=None))

    assert model.events == []


@pytest.mark.asyncio
async def test_audio_review_request_triggers_fallback_even_when_status_is_clear(monkeypatch):
    model = _FakeChatModel(
        _custom_result(transcript_status="clear", needs_audio_review=True),
        _custom_result(transcript_status="clear"),
    )
    agent = LangChainPostCallAgent(model)
    stt_calls = []

    async def transcribe(_call):
        stt_calls.append(True)
        return "Independent recording transcript."

    monkeypatch.setattr(agent, "_transcribe_recording", transcribe)

    await agent.process(_call())

    assert stt_calls == [True]
    assert len(model.messages) == 2


@pytest.mark.parametrize("language", ["ta-IN", "te-IN", "ml-IN", "en-IN", "kn-IN"])
@pytest.mark.asyncio
async def test_supported_languages_automatically_get_audio_review(monkeypatch, language):
    model = _FakeChatModel(
        _custom_result(transcript_status="clear"),
        _custom_result(transcript_status="clear"),
    )
    agent = LangChainPostCallAgent(model)
    stt_calls = []

    async def transcribe(call):
        stt_calls.append(call.live_agent_language)
        return "Independent recording transcript."

    monkeypatch.setattr(agent, "_transcribe_recording", transcribe)

    result = await agent.process(_call(language=language))

    assert stt_calls == [language]
    assert len(model.messages) == 2
    assert result.stt_status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ta-IN", "ml-IN", "te-IN"])
async def test_regional_languages_trigger_automatic_review_without_workflow_dispatch(
    monkeypatch, language
):
    model = _FakeChatModel(
        _custom_result(transcript_status="clear"),
        _custom_result(transcript_status="clear"),
    )
    class _UnrelatedWorkflow:
        workflow_id = "test.unrelated"

        async def process(self, _call, _result):
            raise AssertionError("no workflow ID was configured for this call")

    registry = PostCallWorkflowRegistry()
    registry.register(_UnrelatedWorkflow())
    agent = LangChainPostCallAgent(model, workflow_registry=registry)
    transcribed = []

    async def transcribe(_call):
        transcribed.append(True)
        return "Caller: independent transcript"

    monkeypatch.setattr(agent, "_transcribe_recording", transcribe)

    result = await agent.process(_call(language=language))

    assert transcribed == [True]
    assert model.events == ["analysis", "analysis"]
    assert result.stt_status == "completed"
    assert result.workflow_result is None


@pytest.mark.asyncio
async def test_stt_failure_raises_retryable_error_after_first_analysis(monkeypatch):
    first_result = _custom_result(transcript_status="unclear")
    model = _FakeChatModel(first_result)
    agent = LangChainPostCallAgent(model)

    async def transcribe(_call):
        raise RuntimeError("speech service unavailable")

    monkeypatch.setattr(agent, "_transcribe_recording", transcribe)

    with pytest.raises(PostCallTranscriptionError) as error:
        await agent.process(_call())

    assert len(model.messages) == 1
    assert error.value.stt_status == "failed"
    assert error.value.reason == "unclear_transcript_transcription_failed"


@pytest.mark.asyncio
async def test_platform_assessment_does_not_change_client_result_shape():
    model = _FakeChatModel(_custom_result(transcript_status="clear"))
    agent = LangChainPostCallAgent(model)

    # Isolate output shape from the automatic English audio review path.
    result = await agent.process(_call(language="fr-FR"))

    assert result.structured_output == {
        "outcome": "checked_out",
        "transcript_summary": "Caller confirmed duty completion.",
    }
    # Platform quality/status fields stay separate from the client's schema output.
    assert "transcript_status" not in result.structured_output
    assert set(result.structured_output) == set(_OUTPUT_SCHEMA["properties"])


def test_call_status_reports_recording_only_work_as_queued_for_stt():
    status = _post_call_pipeline_fields(
        {
            "post_call_agent_enabled": True,
            "status": "completed",
            "transcript_available": False,
            "recording_available": True,
        },
        None,
    )

    assert status == {
        "post_call_status": "queued",
        "post_call_transcript_status": "pending",
        "post_call_stt_status": "pending",
        "post_call_decision_reason": "missing_live_transcript",
    }


def test_call_status_reports_missing_transcript_and_recording_as_no_data():
    status = _post_call_pipeline_fields(
        {
            "post_call_agent_enabled": True,
            "status": "completed",
            "transcript_available": False,
            "recording_available": False,
        },
        None,
    )

    assert status == {
        "post_call_status": "skipped_no_data",
        "post_call_transcript_status": "no_data",
        "post_call_stt_status": "unavailable",
        "post_call_decision_reason": "missing_transcript_and_recording",
    }


def test_call_status_reads_persisted_quality_and_stt_statuses():
    status = _post_call_pipeline_fields(
        {"post_call_agent_enabled": True, "status": "completed"},
        {
            "status": "completed",
            "transcript_status": "unclear",
            "stt_status": "failed",
            "decision_reason": "unclear_transcript_transcription_failed",
        },
    )

    assert status == {
        "post_call_status": "completed",
        "post_call_transcript_status": "unclear",
        "post_call_stt_status": "failed",
        "post_call_decision_reason": "unclear_transcript_transcription_failed",
    }


@pytest.mark.asyncio
async def test_coordinator_queues_recording_only_call_for_stt_first():
    class Dispatcher:
        def __init__(self):
            self.calls = []

        async def enqueue(self, call):
            self.calls.append(call)

    class CallStore:
        async def mark_post_call_enqueued(self, tenant_id, session_id):
            return None

    dispatcher = Dispatcher()
    coordinator = CallCoordinator(
        {},
        subscriptions=None,
        post_call_dispatcher=dispatcher,
        call_session_store=CallStore(),
        post_call_required=True,
    )
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        tenant_id="tenant-1",
        metadata={"post_call_transcript": "", "recording_uri": "recording://call-1"},
    )

    await coordinator._enqueue_post_call(session, datetime.now(UTC))

    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0].transcript == ""
    assert dispatcher.calls[0].recording_uri == "recording://call-1"
    assert session.metadata["post_call_enqueued"] is True
