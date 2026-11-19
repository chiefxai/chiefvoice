"""Content-free audio flow diagnostics for live calls."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from pipecat.audio.resamplers.soxr_stream_resampler import SOXRStreamAudioResampler
from pipecat.frames.frames import InputAudioRawFrame, InterruptionFrame, OutputAudioRawFrame
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessor,
    FrameProcessorSetup,
)
from pipecat.serializers.base_serializer import FrameSerializer

from chiefvoice.sessions import AcceptedSession

logger = logging.getLogger(__name__)


@dataclass
class _AudioStage:
    frames: int = 0
    audio_bytes: int = 0
    wire_bytes: int = 0
    sample_rates: set[int] = field(default_factory=set)


class CallAudioDiagnostics:
    """Count audio at each boundary without storing or logging audio content."""

    def __init__(self, session: AcceptedSession):
        self.session_id = session.session_id
        self._stages: dict[str, _AudioStage] = {}
        self._started_at = time.monotonic()
        self._gemini_audio = {
            "received": 0,
            "queued": 0,
            "sent": 0,
            "dropped": 0,
        }
        self._gemini_reconnects = 0
        self._vad_events = {
            "caller_speech_started": 0,
            "caller_speech_stopped": 0,
            "caller_speech_detected_by_gemini_transcription": 0,
            "auto_hangup_completion_phrase_detected": 0,
            "auto_hangup_completion_phrase_rejected": 0,
            "gemini_interruption_accepted": 0,
            "gemini_interruption_suppressed": 0,
            "gemini_echo_interruption_suppressed": 0,
            "response_watchdog_fired": 0,
            "response_watchdog_recovered": 0,
            "response_watchdog_fallback": 0,
            "caller_inactivity_timeout": 0,
            "caller_inactivity_first_prompt": 0,
            "caller_inactivity_second_prompt": 0,
            "caller_inactivity_policy_terminated": 0,
            "hangup_validation_passed": 0,
            "hangup_validation_rejected": 0,
            "hangup_premature_rejected": 0,
            "hangup_idempotent_duplicate": 0,
            "closing_audio_drain_completed": 0,
            "vobiz_playback_checkpoint_sent": 0,
            "vobiz_playback_checkpoint_send_failed": 0,
            "vobiz_playback_ack_received": 0,
            "vobiz_playback_ack_timeout": 0,
            "vobiz_playback_interrupted": 0,
            "vobiz_playback_clear_received": 0,
        }

    def record_gemini_audio(self, outcome: str, *, frames: int = 1) -> None:
        """Count model-bound frames, including frames lost during reconnects."""
        if outcome not in self._gemini_audio:
            raise ValueError(f"Unsupported Gemini audio outcome: {outcome}")
        self._gemini_audio[outcome] += max(0, frames)

    def record_gemini_reconnect(self) -> int:
        self._gemini_reconnects += 1
        return self._gemini_reconnects

    def record_conversation_event(self, event: str, *, count: int = 1) -> None:
        if event not in self._vad_events:
            raise ValueError(f"Unsupported conversation diagnostic: {event}")
        self._vad_events[event] += max(0, count)

    def record(
        self,
        stage: str,
        audio_bytes: int,
        sample_rate: int | None,
        wire_bytes: int = 0,
    ) -> None:
        stats = self._stages.setdefault(stage, _AudioStage())
        stats.frames += 1
        stats.audio_bytes += max(0, audio_bytes)
        stats.wire_bytes += max(0, wire_bytes)
        if sample_rate:
            stats.sample_rates.add(sample_rate)
        if stats.frames == 1:
            extra = {
                "session_id": self.session_id,
                "stage": stage,
                "audio_bytes": audio_bytes,
                "sample_rate": sample_rate,
                "elapsed_since_pipeline_ms": round((time.monotonic() - self._started_at) * 1000),
            }
            logger.info(
                "call_audio_first_frame",
                extra=extra,
            )

    def record_frame(
        self,
        stage: str,
        frame: InputAudioRawFrame | OutputAudioRawFrame,
        wire_bytes: int = 0,
    ) -> None:
        self.record(stage, len(frame.audio), frame.sample_rate, wire_bytes)

    def log_summary(self) -> None:
        stages: dict[str, dict[str, Any]] = {
            name: {
                "frames": stats.frames,
                "audio_bytes": stats.audio_bytes,
                "wire_bytes": stats.wire_bytes,
                "sample_rates": sorted(stats.sample_rates),
            }
            for name, stats in self._stages.items()
        }
        for name in (
            "vobiz_inbound_decoded",
            "gemini_input",
            "gemini_output",
            "vobiz_outbound_serialized",
        ):
            stages.setdefault(
                name,
                {"frames": 0, "audio_bytes": 0, "wire_bytes": 0, "sample_rates": []},
            )
        logger.info(
            "call_audio_flow_summary",
            extra={
                "session_id": self.session_id,
                "audio_stages": stages,
                "gemini_audio_frames": dict(self._gemini_audio),
                "gemini_reconnect_attempts": self._gemini_reconnects,
                "conversation_events": dict(self._vad_events),
            },
        )


class AudioFrameCounter(FrameProcessor):
    """Observe a Pipecat audio boundary and pass every frame through unchanged."""

    def __init__(self, diagnostics: CallAudioDiagnostics, stage: str, frame_type: type):
        super().__init__()
        self._diagnostics = diagnostics
        self._stage = stage
        self._frame_type = frame_type

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, self._frame_type):
            self._diagnostics.record_frame(self._stage, frame)
        await self.push_frame(frame, direction)


class GeminiInputResampler(FrameProcessor):
    """Convert telephony PCM to Gemini Live's required 16 kHz input rate."""

    def __init__(self, target_sample_rate: int = 16000):
        super().__init__()
        self._target_sample_rate = target_sample_rate
        # Keep resampler state across frames to avoid discontinuities at chunk boundaries.
        self._resampler = SOXRStreamAudioResampler(quality="LQ", clear_after_secs=None)

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if (
            not isinstance(frame, InputAudioRawFrame)
            or frame.sample_rate == self._target_sample_rate
        ):
            await self.push_frame(frame, direction)
            return
        if frame.num_channels != 1:
            logger.error(
                "gemini_audio_resample_unsupported_channels",
                extra={"channels": frame.num_channels, "sample_rate": frame.sample_rate},
            )
            return
        audio = await self._resampler.resample(
            frame.audio, frame.sample_rate, self._target_sample_rate
        )
        if audio:
            await self.push_frame(
                InputAudioRawFrame(
                    audio=audio,
                    sample_rate=self._target_sample_rate,
                    num_channels=frame.num_channels,
                ),
                direction,
            )


class CallerAudioGain(FrameProcessor):
    """Apply configurable gain to inbound signed 16-bit PCM with peak limiting."""

    def __init__(self, gain_db: float = 6.0):
        super().__init__()
        if not 0.0 <= gain_db <= 24.0:
            raise ValueError("Caller audio gain must be between 0 and 24 dB")
        self._linear_gain = 10 ** (gain_db / 20)

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if not isinstance(frame, InputAudioRawFrame) or self._linear_gain == 1.0:
            await self.push_frame(frame, direction)
            return
        if len(frame.audio) % 2:
            logger.warning(
                "caller_audio_gain_skipped_invalid_pcm",
                extra={"audio_bytes": len(frame.audio)},
            )
            await self.push_frame(frame, direction)
            return

        # Vobiz's decoded audio is signed 16-bit little-endian PCM. Saturating
        # each sample prevents boosted speech peaks from wrapping and distorting.
        amplified = bytearray(len(frame.audio))
        for offset in range(0, len(frame.audio), 2):
            sample = int.from_bytes(frame.audio[offset : offset + 2], "little", signed=True)
            sample = max(-32768, min(32767, round(sample * self._linear_gain)))
            amplified[offset : offset + 2] = sample.to_bytes(2, "little", signed=True)
        frame.audio = bytes(amplified)
        await self.push_frame(frame, direction)


class ObservedVobizSerializer(FrameSerializer):
    """Wrap the Vobiz serializer and count decoded/encoded audio frames."""

    def __init__(self, serializer: Any, diagnostics: CallAudioDiagnostics, playback_tracker=None):
        super().__init__()
        self._serializer = serializer
        self._diagnostics = diagnostics
        self._playback_tracker = playback_tracker

    async def setup(self, setup: FrameProcessorSetup) -> None:
        await self._serializer.setup(setup)

    async def deserialize(self, data: str | bytes):
        # Pipecat's Vobiz serializer currently only decodes media frames. Keep
        # provider playback receipts visible before delegating to it.
        if self._playback_tracker is not None:
            try:
                payload = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
                if isinstance(payload, dict) and payload.get("event") == "playedStream":
                    self._playback_tracker.acknowledge(payload.get("name"))
                elif isinstance(payload, dict) and payload.get("event") == "clearedAudio":
                    self._diagnostics.record_conversation_event("vobiz_playback_clear_received")
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                pass
        frame = await self._serializer.deserialize(data)
        if isinstance(frame, InputAudioRawFrame):
            wire_bytes = len(data.encode("utf-8")) if isinstance(data, str) else len(data)
            self._diagnostics.record_frame("vobiz_inbound_decoded", frame, wire_bytes)
        return frame

    async def serialize(self, frame):
        if self._playback_tracker is not None and isinstance(frame, InterruptionFrame):
            self._playback_tracker.mark_interrupted()
        payload = await self._serializer.serialize(frame)
        if isinstance(frame, OutputAudioRawFrame):
            if payload is None:
                logger.warning(
                    "vobiz_audio_output_not_serialized",
                    extra={"session_id": self._diagnostics.session_id},
                )
            else:
                payload_bytes = (
                    len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
                )
                self._diagnostics.record_frame("vobiz_outbound_serialized", frame, payload_bytes)
        return payload


def diagnostics_for_session(session: AcceptedSession) -> CallAudioDiagnostics:
    diagnostics = session.metadata.get("call_audio_diagnostics")
    if not isinstance(diagnostics, CallAudioDiagnostics):
        diagnostics = CallAudioDiagnostics(session)
        session.metadata["call_audio_diagnostics"] = diagnostics
    return diagnostics
