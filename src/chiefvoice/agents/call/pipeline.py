from collections.abc import Sequence

from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.transports.base_transport import BaseTransport

from chiefvoice.agents.call.audio_diagnostics import (
    AudioFrameCounter,
    CallAudioDiagnostics,
    GeminiInputResampler,
)
from chiefvoice.agents.post_call.transcript import TranscriptCollector


def build_pipeline(
    transport: BaseTransport,
    conversation_processors: Sequence[FrameProcessor],
    *,
    recording_processor: FrameProcessor | None = None,
    transcript_processor: TranscriptCollector | None = None,
    assistant_transcript_processor: TranscriptCollector | None = None,
    audio_diagnostics: CallAudioDiagnostics | None = None,
) -> Pipeline:
    """Assemble a channel-neutral Pipecat pipeline around provider transport I/O.

    The caller supplies the chosen STT/LLM/TTS or realtime processors. Recording is
    attached after transport output so it can observe both inbound and outbound audio.
    The context aggregators initialize LLM services with an LLMContextFrame before
    realtime audio reaches them and maintain conversation state for post-call capture.
    """
    # Start the first spoken turn as soon as Gemini is connected. Without an
    # initial prompt, a realtime model waits for caller speech and server VAD,
    # which feels like a several-second dead air on phone calls.
    context = LLMContext(messages=[{
        "role": "user",
        "content": "The phone call has just connected. Greet the caller briefly in the configured language and ask the first question needed for this call's task.",
    }])
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    # Vobiz supplies narrowband 8 kHz PCM, while Gemini Live requires 16 kHz
    # PCM input. Resample before both diagnostics and the realtime model.
    processors: list[FrameProcessor] = [transport.input(), GeminiInputResampler(), user_aggregator]
    # Gemini emits caller transcription frames upstream. Put the collector
    # after the LLM and before the user aggregator: the aggregator consumes
    # TranscriptionFrame, so a tap before it never sees the text.
    if transcript_processor is not None:
        processors.append(transcript_processor)
    if audio_diagnostics is not None:
        # Count at the actual model input boundary, after the context
        # aggregator, so the metric catches frames the model can consume.
        processors.append(
            AudioFrameCounter(audio_diagnostics, "gemini_input", InputAudioRawFrame)
        )
    processors.extend(conversation_processors)
    if assistant_transcript_processor is not None:
        processors.append(assistant_transcript_processor)
    if audio_diagnostics is not None:
        processors.append(
            AudioFrameCounter(audio_diagnostics, "gemini_output", OutputAudioRawFrame)
        )
    processors.append(transport.output())
    if recording_processor is not None:
        processors.append(recording_processor)
    processors.append(assistant_aggregator)
    return Pipeline(processors)
