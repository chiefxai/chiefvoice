from collections.abc import Sequence

from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_transport import BaseTransport

from chiefvoice.agents.post_call.transcript import TranscriptCollector


def build_pipeline(
    transport: BaseTransport,
    conversation_processors: Sequence[FrameProcessor],
    *,
    recording_processor: FrameProcessor | None = None,
    transcript_processor: TranscriptCollector | None = None,
    assistant_transcript_processor: TranscriptCollector | None = None,
) -> Pipeline:
    """Assemble a channel-neutral Pipecat pipeline around provider transport I/O.

    The caller supplies the chosen STT/LLM/TTS or realtime processors. Recording is
    attached after transport output so it can observe both inbound and outbound audio.
    """
    processors: list[FrameProcessor] = [transport.input()]
    if transcript_processor is not None:
        processors.append(transcript_processor)
    processors.extend(conversation_processors)
    if assistant_transcript_processor is not None:
        processors.append(assistant_transcript_processor)
    processors.append(transport.output())
    if recording_processor is not None:
        processors.append(recording_processor)
    return Pipeline(processors)
