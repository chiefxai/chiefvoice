import inspect
from collections.abc import Awaitable, Callable, Sequence

from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_transport import BaseTransport

from chiefvoice.agents.call.audio_diagnostics import diagnostics_for_session
from chiefvoice.agents.call.pipeline import build_pipeline
from chiefvoice.agents.call.providers import RealtimeAgentRegistry
from chiefvoice.agents.post_call.transcript import TranscriptCollector
from chiefvoice.recordings.storage import (
    RecordingStore,
    attach_recording,
    create_recording_processor,
)
from chiefvoice.sessions import AcceptedSession

ProcessorFactory = Callable[
    [AcceptedSession], Sequence[FrameProcessor] | Awaitable[Sequence[FrameProcessor]]
]


def create_realtime_pipeline_factory(
    registry: RealtimeAgentRegistry,
    provider_name: str,
    recording_store: RecordingStore | None = None,
) -> Callable[[AcceptedSession, BaseTransport], Awaitable[Pipeline]]:
    """Create a shared pipeline factory around a replaceable realtime model provider."""
    provider = registry.get(provider_name)
    return create_voice_pipeline_factory(provider.create_processors, recording_store)


def create_voice_pipeline_factory(
    processor_factory: ProcessorFactory,
    recording_store: RecordingStore | None = None,
) -> Callable[[AcceptedSession, BaseTransport], Awaitable[Pipeline]]:
    """Build the shared Pipecat pipeline and attach opt-in call recording."""

    async def factory(session: AcceptedSession, transport: BaseTransport) -> Pipeline:
        conversation_processors = processor_factory(session)
        if inspect.isawaitable(conversation_processors):
            conversation_processors = await conversation_processors

        recorder = None
        if recording_store is not None:
            recorder = create_recording_processor(session)
            attach_recording(recorder, session, recording_store)
        caller_transcript, assistant_transcript = TranscriptCollector.for_pipeline(session)
        return build_pipeline(
            transport,
            conversation_processors,
            recording_processor=recorder,
            transcript_processor=caller_transcript,
            assistant_transcript_processor=assistant_transcript,
            audio_diagnostics=diagnostics_for_session(session),
            bot_speech_drain_event=session.metadata.get("bot_speech_drain_event"),
            session=session,
        )

    return factory
