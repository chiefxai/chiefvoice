"""Bounded audio preservation and controlled reconnects for Gemini Live."""

from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import time
import wave
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher
from importlib.resources import files
from pathlib import Path
from typing import Any

from google.genai.types import Blob
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    InputAudioRawFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)

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
            self._chiefvoice_ready_event = asyncio.Event()
            self._chiefvoice_connection_open_event = asyncio.Event()
            self._chiefvoice_connection_error_event = asyncio.Event()
            self._chiefvoice_connection_error_type: str | None = None
            self._chiefvoice_last_ready = False
            self._chiefvoice_fallback_active = False
            self._chiefvoice_terminal = False
            self._chiefvoice_recovery_reconnect_task: asyncio.Task | None = None
            from chiefvoice.core.config import settings

            self._chiefvoice_recovery_ready_timeout_seconds = (
                settings.gemini_recovery_ready_timeout_seconds
            )
            self._chiefvoice_recovery_superseded = False
            self._chiefvoice_recent_input_transcript = ""
            self._chiefvoice_recent_input_transcript_at = 0.0
            self._chiefvoice_recent_output_transcript = ""
            self._chiefvoice_recent_output_transcript_at = 0.0
            self._chiefvoice_pending_interruption = False
            self._chiefvoice_pending_interruption_task: asyncio.Task | None = None
            self._chiefvoice_preconnected = False
            self._chiefvoice_audio_input_frames = 0
            super().__init__(*args, **kwargs)

        async def setup(self, setup: Any) -> None:
            """Attach a pre-dial connection to Pipecat without reconnecting it."""
            if self._chiefvoice_preconnected:
                # The preflight already ran full setup with the same TaskManager.
                # Re-running the base setup would create a second Gemini websocket
                # and duplicate metrics/tasks. Rebind the pipeline setup metadata only.
                self._setup = setup
                logger.info(
                    "gemini_preconnected_service_attached",
                    extra={"session_id": session_id, "provider": "vertex"},
                )
                return
            await super().setup(setup)

        async def _connect(self, session_resumption_handle: str | None = None) -> None:
            # A reconnect that outlives call teardown must not create a fresh
            # Gemini connection after the media pipeline has ended.
            if self._chiefvoice_terminal or self._chiefvoice_fallback_active:
                return
            await super()._connect(session_resumption_handle=session_resumption_handle)

        async def stop(self, frame: EndFrame) -> None:
            self._chiefvoice_terminal = True
            await super().stop(frame)

        async def cancel(self, frame: CancelFrame) -> None:
            self._chiefvoice_terminal = True
            await super().cancel(frame)

        async def cleanup(self) -> None:
            self._chiefvoice_terminal = True
            await super().cleanup()

        async def _handle_connection_error(self, error: Exception) -> bool:
            # The receive loop waits here before reconnecting. Buffer new
            # caller audio during that wait instead of writing to the closed socket.
            self._ready_for_realtime_input = False
            self._chiefvoice_ready_event.clear()
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

        async def _cancel_recovery_reconnect(self) -> None:
            task = self._chiefvoice_recovery_reconnect_task
            if task is None or task is asyncio.current_task():
                return
            if task.done():
                try:
                    task.result()
                except (asyncio.CancelledError, Exception):
                    pass
                self._clear_recovery_reconnect_task(task)
                return
            task.cancel()
            # Task cancellation should be prompt for a Pipecat reconnect. Keep
            # ownership via the field and done callback if a provider coroutine
            # delays cancellation; never block call teardown without a limit.
            done, _ = await asyncio.wait({task}, timeout=1.0)
            if task in done:
                try:
                    task.result()
                except (asyncio.CancelledError, Exception):
                    pass
                self._clear_recovery_reconnect_task(task)
            else:
                logger.error(
                    "gemini_reconnect_cancellation_timed_out",
                    extra={"session_id": session_id, "provider": "vertex"},
                )

        async def _cancel_pending_connection(self) -> None:
            task = getattr(self, "_connection_task", None)
            if task is None or task is asyncio.current_task() or task.done():
                return
            await self.cancel_task(task, timeout=1.0)
            if task.done() and getattr(self, "_connection_task", None) is task:
                self._connection_task = None

        def _clear_recovery_reconnect_task(self, task: asyncio.Task) -> None:
            if self._chiefvoice_recovery_reconnect_task is task:
                self._chiefvoice_recovery_reconnect_task = None
                self._chiefvoice_recovery_superseded = False

        def supersede_recovery_for_caller_speech(self) -> None:
            """Prevent a late watchdog reconnect from replaying stale context."""
            task = self._chiefvoice_recovery_reconnect_task
            if task is not None and not task.done():
                self._chiefvoice_recovery_superseded = True
                logger.info(
                    "gemini_recovery_superseded_by_caller_speech",
                    extra={"session_id": session_id, "provider": "vertex"},
                )

        async def _disconnect(self) -> None:
            was_ready = bool(self._ready_for_realtime_input)
            self._chiefvoice_ready_event.clear()
            recovery_task = self._chiefvoice_recovery_reconnect_task
            if recovery_task is not None and recovery_task is not asyncio.current_task():
                await self._cancel_recovery_reconnect()
            pending_interruption = self._chiefvoice_pending_interruption_task
            self._chiefvoice_pending_interruption_task = None
            self._chiefvoice_pending_interruption = False
            if pending_interruption is not None and not pending_interruption.done():
                await self.cancel_task(pending_interruption, timeout=1.0)
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
            self._chiefvoice_audio_input_frames += 1
            if self._chiefvoice_audio_input_frames == 1:
                logger.info(
                    "gemini_audio_received_by_service",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "audio_bytes": len(frame.audio),
                        "sample_rate": frame.sample_rate,
                        "ready_for_audio": bool(self._ready_for_realtime_input),
                    },
                )
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
                diagnostics.record_gemini_audio("preroll_buffered")
                return
            diagnostics.record_gemini_audio("sent")
            return True

        async def _handle_session_ready(self, session: Any) -> None:
            if self._chiefvoice_terminal or self._chiefvoice_fallback_active:
                logger.warning(
                    "gemini_late_session_ignored_after_call_shutdown",
                    extra={"session_id": session_id, "provider": "vertex"},
                )
                await session.close()
                return
            await super()._handle_session_ready(session)
            # This event means the provider accepted the Live API connection.
            # It is deliberately distinct from ready_for_realtime_input, which
            # also requires a seeded conversation context during normal calls.
            self._chiefvoice_connection_open_event.set()
            self._chiefvoice_last_ready = bool(self._ready_for_realtime_input)
            if self._chiefvoice_last_ready:
                self._chiefvoice_ready_event.set()
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

        async def _create_initial_response(self, for_reconnect: bool = False) -> None:
            if for_reconnect and self._chiefvoice_recovery_superseded:
                # Pipecat's Gemini 2.5 reconnect path automatically triggers an
                # inference after seeding retained context. Caller speech that
                # arrived during the reconnect supersedes that stale inference;
                # seed without turn completion so the next caller turn drives it.
                inference_on_context_initialization = self._inference_on_context_initialization
                self._inference_on_context_initialization = False
                try:
                    await super()._create_initial_response(for_reconnect=False)
                finally:
                    self._inference_on_context_initialization = inference_on_context_initialization
                logger.info(
                    "gemini_reconnect_stale_inference_suppressed",
                    extra={"session_id": session_id, "provider": "vertex"},
                )
                return
            await super()._create_initial_response(for_reconnect=for_reconnect)

        async def push_error(self, error_msg: str, exception: Exception | None = None, *args: Any, **kwargs: Any):
            if not self._chiefvoice_connection_open_event.is_set():
                self._chiefvoice_connection_error_type = (
                    type(exception).__name__ if exception is not None else "GeminiConnectionError"
                )
                self._chiefvoice_connection_error_event.set()
            await super().push_error(error_msg, exception, *args, **kwargs)

        async def broadcast_interruption(self) -> None:
            if self._chiefvoice_fallback_active:
                return
            # Gemini's server VAD can interpret handset/line echo as caller
            # speech. Require independent local Silero VAD confirmation before
            # canceling the assistant's current audio.
            if not self._user_is_speaking:
                diagnostics.record_conversation_event("gemini_interruption_suppressed")
                logger.info(
                    "gemini_interruption_suppressed_without_local_speech",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "bot_responding": bool(self._bot_is_responding),
                        "session_ready": bool(self._ready_for_realtime_input),
                    },
                )
                return
            if not self._bot_is_responding:
                return
            if self._chiefvoice_pending_interruption:
                return
            self._chiefvoice_pending_interruption = True
            self._chiefvoice_pending_interruption_task = self.create_task(
                self._confirm_interruption_after_echo_check(),
                name="gemini_interruption_echo_check",
            )

        async def _confirm_interruption_after_echo_check(self) -> None:
            try:
                # Input transcription may arrive in the next server message.
                # A short delay lets us compare it with recent assistant speech
                # before stopping the audio currently being played.
                await asyncio.sleep(0.20)
                if (
                    self._disconnecting
                    or self._chiefvoice_fallback_active
                    or not self._bot_is_responding
                ):
                    return
                now = time.monotonic()
                input_text = self._chiefvoice_recent_input_transcript
                output_text = self._chiefvoice_recent_output_transcript
                recent_echo_candidate = (
                    now - self._chiefvoice_recent_input_transcript_at <= 2.0
                    and now - self._chiefvoice_recent_output_transcript_at <= 5.0
                    and self._transcript_looks_like_echo(input_text, output_text)
                )
                if recent_echo_candidate:
                    diagnostics.record_conversation_event("gemini_echo_interruption_suppressed")
                    logger.warning(
                        "gemini_interruption_suppressed_as_telephone_echo",
                        extra={
                            "session_id": session_id,
                            "provider": "vertex",
                            "comparison_window_ms": 200,
                        },
                    )
                    return
                diagnostics.record_conversation_event("gemini_interruption_accepted")
                logger.info(
                    "gemini_interruption_confirmed_by_local_vad",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "bot_responding": bool(self._bot_is_responding),
                    },
                )
                await super().broadcast_interruption()
            finally:
                self._chiefvoice_pending_interruption = False
                if self._chiefvoice_pending_interruption_task is asyncio.current_task():
                    self._chiefvoice_pending_interruption_task = None

        @staticmethod
        def _transcript_looks_like_echo(caller_text: str, assistant_text: str) -> bool:
            def normalize(value: str) -> str:
                return " ".join(re.findall(r"[^\W_]+", value.casefold()))

            caller = normalize(caller_text)
            assistant = normalize(assistant_text)
            if len(caller) < 8 or len(assistant) < 8:
                return False
            if caller in assistant or assistant in caller:
                return True
            return SequenceMatcher(None, caller, assistant).ratio() >= 0.90

        async def _handle_msg_model_turn(self, message: Any) -> None:
            if self._chiefvoice_fallback_active:
                logger.warning(
                    "gemini_late_response_suppressed_during_fallback",
                    extra={"session_id": session_id, "provider": "vertex"},
                )
                return
            await super()._handle_msg_model_turn(message)

        async def _handle_msg_output_transcription(self, message: Any) -> None:
            if self._chiefvoice_fallback_active:
                return
            server_content = getattr(message, "server_content", None)
            output = getattr(server_content, "output_transcription", None)
            text = getattr(output, "text", None)
            if text:
                if not self._bot_is_responding:
                    self._chiefvoice_recent_output_transcript = ""
                self._chiefvoice_recent_output_transcript = (
                    f"{self._chiefvoice_recent_output_transcript} {text}".strip()
                )[-1000:]
                self._chiefvoice_recent_output_transcript_at = time.monotonic()
            await super()._handle_msg_output_transcription(message)

        async def _handle_msg_input_transcription(self, message: Any) -> None:
            server_content = getattr(message, "server_content", None)
            incoming = getattr(server_content, "input_transcription", None)
            text = getattr(incoming, "text", None)
            if text:
                if not self._user_transcription_buffer:
                    self._chiefvoice_recent_input_transcript = ""
                self._chiefvoice_recent_input_transcript = (
                    f"{self._chiefvoice_recent_input_transcript} {text}".strip()[-1000:]
                )
                self._chiefvoice_recent_input_transcript_at = time.monotonic()
            await super()._handle_msg_input_transcription(message)

        async def recover_unanswered_turn(self) -> bool:
            """Restart a silent realtime session and resume its pending turn.

            A websocket can remain marked ready even when Gemini stops producing
            turns. For a watchdog recovery, readiness alone is not proof that the
            session is healthy, so perform one serialized reconnect before retrying.
            """
            if (
                self._disconnecting
                or self._chiefvoice_terminal
                or self._chiefvoice_fallback_active
            ):
                return False
            deadline = asyncio.get_running_loop().time() + (
                self._chiefvoice_recovery_ready_timeout_seconds
            )
            try:
                self._chiefvoice_recovery_superseded = False
                had_resumption_handle = bool(self._session_resumption_handle)
                reconnect_will_generate_response = bool(
                    self._context
                    and not had_resumption_handle
                    and not getattr(self, "_is_gemini_3", False)
                )
                generation_was_active = bool(self._bot_is_responding)
                if generation_was_active:
                    await asyncio.wait_for(
                        self._handle_interruption(),
                        timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
                    )

                # A stale but truthy readiness flag was the gap in the previous
                # recovery path. Reopen the same Pipecat service/session once;
                # its context seeding or session resumption preserves history.
                self._chiefvoice_ready_event.clear()
                logger.warning(
                    "gemini_unanswered_turn_reconnect_started",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "ready_before_recovery": bool(self._ready_for_realtime_input),
                        "generation_interrupted": generation_was_active,
                        "session_resumption_available": had_resumption_handle,
                    },
                )
                reconnect_task = self.create_task(
                    self._reconnect(), name=f"gemini-watchdog-reconnect-{session_id}"
                )
                self._chiefvoice_recovery_reconnect_task = reconnect_task
                reconnect_task.add_done_callback(self._clear_recovery_reconnect_task)
                await asyncio.wait_for(
                    asyncio.shield(reconnect_task),
                    timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
                )
                await asyncio.wait_for(
                    self._chiefvoice_ready_event.wait(),
                    timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
                )
                if not self._session or not self._ready_for_realtime_input:
                    return False

                # Re-seeding Gemini 2.5 without a resumption handle triggers an
                # inference over the retained Pipecat context. Other reconnect
                # modes need an explicit prompt to continue the pending turn.
                if not reconnect_will_generate_response:
                    await asyncio.wait_for(
                        self._session.send_realtime_input(
                            text=(
                                "Continue from the current conversation state. Do not repeat any "
                                "question or instruction the caller has already heard. Use the "
                                "caller's latest answer and the information already collected; "
                                "ask only for the next missing required item. If the caller's "
                                "latest answer already addressed the pending question, "
                                "acknowledge it briefly and continue."
                            )
                        ),
                        timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
                    )
                logger.warning(
                    "gemini_unanswered_turn_recovery_requested",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "connection_ready": True,
                        "reconnected": True,
                        "prompt_sent": not reconnect_will_generate_response,
                    },
                )
                diagnostics.record_conversation_event("response_watchdog_recovered")
                return True
            except asyncio.CancelledError:
                await self._cancel_recovery_reconnect()
                raise
            except TimeoutError:
                logger.warning(
                    "gemini_unanswered_turn_recovery_timed_out",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "timeout_seconds": self._chiefvoice_recovery_ready_timeout_seconds,
                    },
                )
                await self._cancel_recovery_reconnect()
                await self._cancel_pending_connection()
                return False
            except Exception as error:
                await self._cancel_recovery_reconnect()
                logger.error(
                    "gemini_unanswered_turn_recovery_failed",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "error_type": type(error).__name__,
                        "error_status": getattr(error, "status", None),
                    },
                )
                return False

        async def prompt_caller_inactivity(self, text: str) -> bool:
            """Prompt Gemini to speak an inactivity / follow-up message to the caller."""
            if (
                self._disconnecting
                or self._chiefvoice_terminal
                or self._chiefvoice_fallback_active
            ):
                return False
            try:
                if not self._session or not self._ready_for_realtime_input:
                    return False
                if self._bot_is_responding:
                    return False
                await self._session.send_realtime_input(text=text)
                logger.info(
                    "gemini_caller_inactivity_prompt_sent",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "prompt": text,
                    },
                )
                return True
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "gemini_caller_inactivity_prompt_failed",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "error_type": type(error).__name__,
                    },
                )
                return False

        async def play_fallback_and_hangup(self) -> bool:
            """Play the packaged/configured PCM fallback once, then end the call."""
            if self._chiefvoice_fallback_active:
                return False
            self._chiefvoice_fallback_active = True
            try:
                await self._cancel_recovery_reconnect()
                await self._cancel_pending_connection()
                if self._bot_is_responding:
                    # The watchdog invokes fallback only after observing no
                    # model audio for the whole recovery window. Stop a stuck
                    # response before injecting the fallback so streams cannot
                    # overlap.
                    await self._handle_interruption()
                from chiefvoice.core.config import settings

                configured_path: Path | None = settings.gemini_fallback_audio_path
                wav_data = (
                    configured_path.read_bytes()
                    if configured_path is not None
                    else files("chiefvoice.agents.call")
                    .joinpath("assets/gemini_unavailable.wav")
                    .read_bytes()
                )
                with wave.open(io.BytesIO(wav_data), "rb") as audio_file:
                    if (
                        audio_file.getnchannels() != 1
                        or audio_file.getsampwidth() != 2
                        or audio_file.getframerate() != 24000
                        or audio_file.getcomptype() != "NONE"
                    ):
                        raise ValueError("Fallback audio must be mono 24 kHz 16-bit PCM WAV")
                    audio = audio_file.readframes(audio_file.getnframes())
                if not audio:
                    raise ValueError("Fallback audio is empty")

                # Mark fallback active before the first await; late Gemini
                # output is suppressed above so it cannot overlap this clip.
                await self.push_frame(TTSStartedFrame())
                await self.push_frame(LLMFullResponseStartFrame())
                await self.push_frame(
                    TTSAudioRawFrame(audio=audio, sample_rate=24000, num_channels=1)
                )
                await self.push_frame(TTSStoppedFrame())
                await self.push_frame(LLMFullResponseEndFrame())
                diagnostics.record_conversation_event("response_watchdog_fallback")
                logger.error(
                    "gemini_fallback_audio_played",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "audio_bytes": len(audio),
                        "sample_rate": 24000,
                    },
                )
                callback = self._chiefvoice_hangup_callback
                hangup_requested = False
                if callable(callback):
                    hangup_requested = await callback() is not False
                if not hangup_requested:
                    logger.error(
                        "gemini_fallback_hangup_unavailable",
                        extra={"session_id": session_id, "provider": "vertex"},
                    )
                return True
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(
                    "gemini_fallback_audio_failed",
                    extra={
                        "session_id": session_id,
                        "provider": "vertex",
                        "error_type": type(error).__name__,
                    },
                )
                callback = self._chiefvoice_hangup_callback
                if callable(callback):
                    try:
                        await callback()
                    except Exception:
                        logger.exception(
                            "gemini_fallback_hangup_failed",
                            extra={"session_id": session_id, "provider": "vertex"},
                        )
                return False

        async def _handle_context(self, context: Any) -> None:
            # The initial context can arrive after the websocket has connected.
            # Pipecat marks realtime input ready only after seeding that context.
            if self._chiefvoice_terminal or self._chiefvoice_fallback_active:
                return
            await super()._handle_context(context)
            if self._ready_for_realtime_input:
                self._chiefvoice_ready_event.set()
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
