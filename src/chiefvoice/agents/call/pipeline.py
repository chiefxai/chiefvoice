import asyncio
from collections.abc import Sequence

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_transport import BaseTransport

from chiefvoice.agents.call.audio_diagnostics import (
    AudioFrameCounter,
    CallAudioDiagnostics,
    CallerAudioGain,
    GeminiInputResampler,
)
from chiefvoice.agents.call.conversation_reliability import GeminiConversationWatchdog
from chiefvoice.agents.post_call.transcript import TranscriptCollector
from chiefvoice.core.config import settings
from chiefvoice.sessions import AcceptedSession


class BotSpeechDrainObserver(FrameProcessor):
    """Track when the transport has finished sending a bot speech turn.

    This processor is deliberately placed after ``transport.output()``. The
    transport emits BotStoppedSpeakingFrame after a normal drain or when
    an interruption discards queued audio. Only a normal drain gets a checkpoint.
    """

    def __init__(self, drain_event: asyncio.Event, checkpoint_callback=None) -> None:
        super().__init__()
        self._drain_event = drain_event
        self._checkpoint_callback = checkpoint_callback
        self._interrupted = False

    async def process_frame(self, frame, direction) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._interrupted = False
            self._drain_event.clear()
        elif isinstance(frame, InterruptionFrame):
            self._interrupted = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if not self._interrupted and self._checkpoint_callback is not None:
                try:
                    await self._checkpoint_callback()
                except Exception:
                    # Checkpoint trouble must not wedge the voice pipeline.
                    pass
            self._drain_event.set()
        await self.push_frame(frame, direction)


def build_pipeline(
    transport: BaseTransport,
    conversation_processors: Sequence[FrameProcessor],
    *,
    recording_processor: FrameProcessor | None = None,
    transcript_processor: TranscriptCollector | None = None,
    assistant_transcript_processor: TranscriptCollector | None = None,
    audio_diagnostics: CallAudioDiagnostics | None = None,
    bot_speech_drain_event: asyncio.Event | None = None,
    session: AcceptedSession | None = None,
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
    context = LLMContext(
        messages=[
            {
                "role": "user",
                "content": (
                    "The phone call has just connected. Greet the caller briefly in the "
                    "configured language and ask the first question needed for this call's task."
                ),
            }
        ]
    )
    local_vad = SileroVADAnalyzer(
        sample_rate=16000,
        params=VADParams(
            # Telephone audio is often much quieter than the desktop audio
            # these defaults are tuned for. Keep the local VAD sensitive
            # enough to agree with Gemini's server-side speech detector.
            confidence=0.65,
            start_secs=0.20,
            stop_secs=0.60,
            min_volume=0.15,
        ),
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=local_vad,
            user_turn_stop_timeout=1.5,
        ),
    )

    # Vobiz supplies narrowband 8 kHz PCM, while Gemini Live requires 16 kHz
    # PCM input. Resample before both diagnostics and the realtime model.
    processors: list[FrameProcessor] = [
        transport.input(),
        CallerAudioGain(settings.caller_audio_gain_db),
        GeminiInputResampler(),
        user_aggregator,
    ]
    # Gemini emits caller transcription frames upstream. Put the collector
    # after the LLM and before the user aggregator: the aggregator consumes
    # TranscriptionFrame, so a tap before it never sees the text.
    if transcript_processor is not None:
        processors.append(transcript_processor)
    if audio_diagnostics is not None:
        # Count at the actual model input boundary, after the context
        # aggregator, so the metric catches frames the model can consume.
        processors.append(AudioFrameCounter(audio_diagnostics, "gemini_input", InputAudioRawFrame))
    model_service = next(
        (
            processor
            for processor in conversation_processors
            if callable(getattr(processor, "recover_unanswered_turn", None))
        ),
        None,
    )
    processors.extend(conversation_processors)
    if session is not None and model_service is not None and audio_diagnostics is not None:
        processors.append(GeminiConversationWatchdog(session, model_service, audio_diagnostics))
        if callable(getattr(model_service, "prompt_caller_inactivity", None)):
            session.metadata["prompt_caller_inactivity"] = model_service.prompt_caller_inactivity
    if assistant_transcript_processor is not None:
        processors.append(assistant_transcript_processor)
    if audio_diagnostics is not None:
        processors.append(
            AudioFrameCounter(audio_diagnostics, "gemini_output", OutputAudioRawFrame)
        )
    processors.append(transport.output())
    if bot_speech_drain_event is not None:
        processors.append(
            BotSpeechDrainObserver(
                bot_speech_drain_event,
                session.metadata.get("send_vobiz_playback_checkpoint") if session else None,
            )
        )
    if recording_processor is not None:
        processors.append(recording_processor)
    processors.append(assistant_aggregator)
    return Pipeline(processors)
