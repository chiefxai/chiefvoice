"""Bounded audio preservation and controlled reconnects for Gemini Live."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from google.genai.types import Blob
from pipecat.frames.frames import InputAudioRawFrame

from chiefvoice.agents.call.audio_diagnostics import CallAudioDiagnostics

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BufferedAudio:
    frame: InputAudioRawFrame
    enqueued_at: float


class BoundedGeminiAudioBuffer:
    """Keep a recent, ordered slice of PCM while a Gemini session is unavailable."""

    def __init__(
        self,
        *,
        max_audio_bytes: int = 64_000,
        max_frames: int = 100,
        max_age_seconds: float = 2.0,
    ) -> None:
        self.max_audio_bytes = max_audio_bytes
        self.max_frames = max_frames
        self.max_age_seconds = max_age_seconds
        self._frames: deque[_BufferedAudio] = deque()
        self._audio_bytes = 0

    def __len__(self) -> int:
        return len(self._frames)

    @property
    def audio_bytes(self) -> int:
        return self._audio_bytes

    def append(
        self,
        frame: InputAudioRawFrame,
        *,
        now: float | None = None,
    ) -> tuple[bool, int]:
        """Append one frame; return whether it was retained and eviction count."""
        timestamp = time.monotonic() if now is None else now
        size = len(frame.audio)
        dropped = self.discard_stale(now=timestamp)
        if size > self.max_audio_bytes:
            return False, dropped + 1
        while self._frames and (
            len(self._frames) >= self.max_frames
            or self._audio_bytes + size > self.max_audio_bytes
        ):
            item = self._frames.popleft()
            self._audio_bytes -= len(item.frame.audio)
            dropped += 1
        self._frames.append(_BufferedAudio(frame=frame, enqueued_at=timestamp))
        self._audio_bytes += size
        return True, dropped

    def pop(self, *, now: float | None = None) -> tuple[InputAudioRawFrame | None, int]:
        """Return the next fresh frame and how many stale frames were discarded."""
        timestamp = time.monotonic() if now is None else now
        dropped = self.discard_stale(now=timestamp)
        if not self._frames:
            return None, dropped
        item = self._frames.popleft()
        self._audio_bytes -= len(item.frame.audio)
        return item.frame, dropped

    def discard_stale(self, *, now: float | None = None) -> int:
        timestamp = time.monotonic() if now is None else now
        dropped = 0
        while self._frames and timestamp - self._frames[0].enqueued_at > self.max_age_seconds:
            item = self._frames.popleft()
            self._audio_bytes -= len(item.frame.audio)
            dropped += 1
        return dropped

    def clear(self) -> int:
        dropped = len(self._frames)
        self._frames.clear()
        self._audio_bytes = 0
        return dropped


def reliable_vertex_service_class(
    base_class: type,
    *,
    session_id: str,
    diagnostics: CallAudioDiagnostics,
) -> type:
    """Build a per-call Gemini service subclass with bounded retry and audio buffering.

    The subclass is per call so its lock, counters, and queue can never be shared
    between calls. Pipecat continues to own exactly one service/session instance.
    """

    class ReliableGeminiLiveVertexLLMService(base_class):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._chiefvoice_audio_buffer = BoundedGeminiAudioBuffer()
            self._chiefvoice_reconnect_lock = asyncio.Lock()
            self._chiefvoice_replay_lock = asyncio.Lock()
            self._chiefvoice_last_ready = False
            super().__init__(*args, **kwargs)

        async def _handle_connection_error(self, error: Exception) -> bool:
            should_retry = await super()._handle_connection_error(error)
            failures = int(getattr(self, "_consecutive_failures", 1))
            details = {
                "session_id": session_id,
                "provider": "vertex",
                "model": getattr(getattr(self, "_settings", None), "model", None),
                "failure_number": failures,
                "max_consecutive_failures": 3,
                "error_type": type(error).__name__,
                "error_code": getattr(error, "code", None),
                "error_status": getattr(error, "status", None),
                "retrying": should_retry,
            }
            logger.warning("gemini_connection_failed", extra=details)
            if not should_retry:
                dropped = self._chiefvoice_audio_buffer.clear()
                if dropped:
                    diagnostics.record_gemini_audio("dropped", frames=dropped)
                    logger.warning(
                        "gemini_audio_buffer_cleared_after_retry_exhaustion",
                        extra={**details, "dropped_frames": dropped},
                    )
                return False

            # Pipecat limits this service to three consecutive failures. Add
            # jittered backoff before its single-session reconnect is started.
            ceiling = min(0.5 * (2 ** max(0, failures - 1)), 4.0)
            delay = random.uniform(ceiling * 0.75, ceiling)
            attempt = diagnostics.record_gemini_reconnect()
            logger.info(
                "gemini_reconnect_scheduled",
                extra={**details, "reconnect_attempt": attempt, "backoff_ms": round(delay * 1000)},
            )
            await asyncio.sleep(delay)
            return True

        async def _reconnect(self) -> None:
            # Context updates and receive-loop errors can race. Serialize them
            # so one Pipecat service never creates overlapping Gemini sessions.
            if self._chiefvoice_reconnect_lock.locked():
                logger.info(
                    "gemini_reconnect_already_in_progress",
                    extra={"session_id": session_id, "provider": "vertex"},
                )
                return
            async with self._chiefvoice_reconnect_lock:
                logger.info(
                    "gemini_reconnect_started",
                    extra={"session_id": session_id, "provider": "vertex"},
                )
                await super()._reconnect()

        async def _disconnect(self) -> None:
            was_ready = bool(self._ready_for_realtime_input)
            await super()._disconnect()
            if was_ready:
                logger.info(
                    "gemini_session_readiness_changed",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "ready_for_audio": False,
                    },
                )

        async def _connection_task_handler(self, config: Any) -> None:
            # Pipecat retries errors raised by the receive loop, but exceptions
            # raised while opening the websocket escape its internal retry loop.
            try:
                await super()._connection_task_handler(config)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self._disconnecting:
                    return
                if await self._handle_connection_error(error):
                    await self._reconnect()
                else:
                    logger.error(
                        "gemini_initial_connection_retry_exhausted",
                        extra={
                            "session_id": session_id,
                            "provider": "vertex",
                            "error_type": type(error).__name__,
                        },
                    )

        async def _send_user_audio(self, frame: InputAudioRawFrame) -> None:
            diagnostics.record_gemini_audio("received")
            if self._audio_input_paused or self._disconnecting:
                diagnostics.record_gemini_audio("dropped")
                logger.debug(
                    "gemini_audio_dropped",
                    extra={"session_id": session_id, "reason": "audio_paused_or_session_closing"},
                )
                return
            if not self._session or not self._ready_for_realtime_input:
                queued, dropped = self._chiefvoice_audio_buffer.append(frame)
                if queued:
                    diagnostics.record_gemini_audio("queued")
                else:
                    logger.warning(
                        "gemini_audio_frame_rejected",
                        extra={
                            "session_id": session_id,
                            "provider": "vertex",
                            "audio_bytes": len(frame.audio),
                            "max_audio_bytes": self._chiefvoice_audio_buffer.max_audio_bytes,
                        },
                    )
                if dropped:
                    diagnostics.record_gemini_audio("dropped", frames=dropped)
                    logger.warning(
                        "gemini_audio_buffer_evicted_frames",
                        extra={
                            "session_id": session_id,
                            "provider": "vertex",
                            "dropped_frames": dropped,
                            "queued_frames": len(self._chiefvoice_audio_buffer),
                            "queued_audio_bytes": self._chiefvoice_audio_buffer.audio_bytes,
                        },
                    )
                return
            await self._chiefvoice_send_audio(frame)

        async def _chiefvoice_send_audio(self, frame: InputAudioRawFrame) -> bool:
            if not self._session or not self._ready_for_realtime_input:
                return False
            if not self._vad_disabled or self._user_is_speaking:
                try:
                    await self._session.send_realtime_input(
                        audio=Blob(
                            data=frame.audio,
                            mime_type=f"audio/pcm;rate={frame.sample_rate}",
                        )
                    )
                except Exception as error:
                    diagnostics.record_gemini_audio("dropped")
                    logger.warning(
                        "gemini_audio_send_failed",
                        extra={
                            "session_id": session_id,
                            "provider": "vertex",
                            "error_type": type(error).__name__,
                            "error_code": getattr(error, "code", None),
                            "error_status": getattr(error, "status", None),
                        },
                    )
                    await self._handle_send_error(error)
                    return False
            else:
                # Match Pipecat's local-VAD pre-roll behavior. These frames are
                # accepted by the Gemini service and sent when speech starts.
                self._user_audio_preroll_buffer.extend(frame.audio)
                preroll_len = int(
                    frame.sample_rate * frame.num_channels * 2 * self._user_audio_preroll_secs
                )
                self._user_audio_preroll_buffer = self._user_audio_preroll_buffer[-preroll_len:]
                self._user_audio_preroll_buffer_sample_rate = frame.sample_rate
            diagnostics.record_gemini_audio("sent")
            return True

        async def _handle_session_ready(self, session: Any) -> None:
            await super()._handle_session_ready(session)
            self._chiefvoice_last_ready = bool(self._ready_for_realtime_input)
            logger.info(
                "gemini_session_readiness_changed",
                extra={
                    "session_id": session_id,
                    "provider": "vertex",
                    "ready_for_audio": self._chiefvoice_last_ready,
                    "queued_frames": len(self._chiefvoice_audio_buffer),
                },
            )
            if self._ready_for_realtime_input:
                await self._chiefvoice_flush_audio()

        async def _handle_context(self, context: Any) -> None:
            # The initial context can arrive after the websocket has connected.
            # Pipecat marks realtime input ready only after seeding that context.
            await super()._handle_context(context)
            if self._ready_for_realtime_input:
                logger.info(
                    "gemini_session_readiness_changed",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "ready_for_audio": True,
                        "ready_source": "context_seeded",
                        "queued_frames": len(self._chiefvoice_audio_buffer),
                    },
                )
                await self._chiefvoice_flush_audio()

        async def _chiefvoice_flush_audio(self) -> None:
            async with self._chiefvoice_replay_lock:
                replayed = 0
                dropped = 0
                while self._ready_for_realtime_input and self._session:
                    frame, stale = self._chiefvoice_audio_buffer.pop()
                    dropped += stale
                    if frame is None:
                        break
                    if not await self._chiefvoice_send_audio(frame):
                        # A failed send is terminal for this attempt. Discard
                        # remaining audio to avoid replaying duplicate/stale
                        # speech on a later call/session.
                        dropped += self._chiefvoice_audio_buffer.clear()
                        break
                    replayed += 1
                if dropped:
                    diagnostics.record_gemini_audio("dropped", frames=dropped)
                logger.info(
                    "gemini_audio_buffer_replayed",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "replayed_frames": replayed,
                        "dropped_frames": dropped,
                        "remaining_frames": len(self._chiefvoice_audio_buffer),
                    },
                )

    return ReliableGeminiLiveVertexLLMService
