"""Turn diagnostics, bounded recovery, and silence management for Gemini Live calls."""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from typing import Any

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
    OutputAudioRawFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from chiefvoice.agents.call.audio_diagnostics import CallAudioDiagnostics
from chiefvoice.core.config import settings
from chiefvoice.sessions import AcceptedSession

logger = logging.getLogger(__name__)


class CallLifecycleState(StrEnum):
    ACTIVE = "active"
    TERMINATING = "terminating"
    TERMINATED = "terminated"


class ConversationTurnState(StrEnum):
    IDLE = "idle"
    CALLER_SPEAKING = "caller_speaking"
    WAITING_FOR_RESPONSE = "waiting_for_response"
    BOT_GENERATING = "bot_generating"
    BOT_SPEAKING = "bot_speaking"
    AWAITING_CALLER = "awaiting_caller"


class GeminiConversationWatchdog(FrameProcessor):
    """Track caller/model states, recover silent Gemini turns, and enforce silence policy.

    The response watchdog recovers a completed caller turn when Gemini goes silent.
    The silence manager monitors caller inactivity, issues staged follow-up prompts,
    resets upon caller speech, and triggers graceful termination when the inactivity policy
    completes.
    """

    def __init__(
        self,
        session: AcceptedSession,
        service: Any,
        diagnostics: CallAudioDiagnostics,
        *,
        response_timeout_seconds: float | None = None,
        recovery_grace_seconds: float | None = None,
        caller_inactivity_seconds: float | None = None,
        caller_inactivity_first_seconds: float | None = None,
        caller_inactivity_second_seconds: float | None = None,
        caller_inactivity_final_seconds: float | None = None,
    ) -> None:
        super().__init__()
        self._session = session
        self._service = service
        self._diagnostics = diagnostics
        self._response_timeout = (
            settings.gemini_response_watchdog_seconds
            if response_timeout_seconds is None
            else response_timeout_seconds
        )
        self._recovery_grace = (
            settings.gemini_recovery_grace_seconds
            if recovery_grace_seconds is None
            else recovery_grace_seconds
        )
        # Inactivity timer configuration with per-session overrides
        self._first_inactivity_timeout = (
            caller_inactivity_first_seconds
            if caller_inactivity_first_seconds is not None
            else (
                caller_inactivity_seconds
                if caller_inactivity_seconds is not None
                else float(
                    session.metadata.get("inactivity_first_timeout")
                    or settings.caller_inactivity_first_timeout_seconds
                )
            )
        )
        self._second_inactivity_timeout = (
            caller_inactivity_second_seconds
            if caller_inactivity_second_seconds is not None
            else (
                caller_inactivity_seconds
                if caller_inactivity_seconds is not None
                else float(
                    session.metadata.get("inactivity_second_timeout")
                    or settings.caller_inactivity_second_timeout_seconds
                )
            )
        )
        self._final_inactivity_timeout = (
            caller_inactivity_final_seconds
            if caller_inactivity_final_seconds is not None
            else (
                caller_inactivity_seconds
                if caller_inactivity_seconds is not None
                else float(
                    session.metadata.get("inactivity_final_timeout")
                    or settings.caller_inactivity_final_timeout_seconds
                )
            )
        )
        self._caller_inactivity_timeout = self._first_inactivity_timeout

        # State tracking
        self._call_state = CallLifecycleState.ACTIVE
        self._turn_state = ConversationTurnState.IDLE
        self._turn_id = 0
        self._inactivity_stage = 0  # 0: silence 1, 1: silence 2, 2: final silence, 3: terminated
        self._caller_is_speaking = False
        self._gemini_is_generating = False
        self._bot_audio_playing = False
        self._response_audio_seen = False
        self._closed = False
        self._watch_task: asyncio.Task | None = None
        self._caller_inactivity_task: asyncio.Task | None = None
        self._recovery_lock = asyncio.Lock()
        self._recovery_attempted_turn: int | None = None

    @property
    def _metadata(self) -> dict[str, Any]:
        meta = getattr(self._session, "metadata", None)
        if meta is None or not isinstance(meta, dict):
            meta = {}
            try:
                self._session.metadata = meta
            except Exception:
                pass
        return meta

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        is_call_terminating = (
            self._call_state in (CallLifecycleState.TERMINATING, CallLifecycleState.TERMINATED)
            or bool(self._metadata.get("call_terminating"))
            or bool(self._metadata.get("call_terminated"))
        )

        if direction is FrameDirection.DOWNSTREAM and isinstance(frame, UserStartedSpeakingFrame):
            if is_call_terminating:
                return
            self._caller_is_speaking = True
            self._inactivity_stage = 0  # Reset silence timer & prompt sequence on caller speech
            self._turn_state = ConversationTurnState.CALLER_SPEAKING
            self._diagnostics.record_conversation_event("caller_speech_started")
            self._cancel_response_watchdog("caller_speech_started")
            self._cancel_caller_inactivity_timer("caller_speech_started")
            logger.info("caller_speech_detected_by_local_vad", extra=self._log_context())

        elif direction is FrameDirection.DOWNSTREAM and isinstance(frame, UserStoppedSpeakingFrame):
            if is_call_terminating:
                return
            self._caller_is_speaking = False
            self._metadata["caller_turn_count"] = self._metadata.get("caller_turn_count", 0) + 1
            self._diagnostics.record_conversation_event("caller_speech_stopped")
            logger.info("caller_turn_completed", extra=self._log_context())
            self._arm_response_watchdog("caller_turn_completed")

        elif direction is FrameDirection.UPSTREAM and isinstance(frame, TranscriptionFrame):
            # Gemini's server VAD can recognize telephone speech that the
            # local VAD misses. Use its finalized input transcription as an
            # activity signal so silence prompts cannot be sent over caller
            # speech, and retain a response watchdog for the missed local turn.
            if not is_call_terminating and frame.text.strip():
                self._inactivity_stage = 0
                self._diagnostics.record_conversation_event(
                    "caller_speech_detected_by_gemini_transcription"
                )
                self._cancel_caller_inactivity_timer("gemini_input_transcription")
                logger.info(
                    "caller_speech_detected_by_gemini_transcription",
                    extra={**self._log_context(), "text_characters": len(frame.text)},
                )
                if not self._caller_is_speaking:
                    self._arm_response_watchdog("gemini_input_transcription")

        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_audio_playing = True
            self._metadata["bot_audio_playing"] = True
            self._turn_state = ConversationTurnState.BOT_SPEAKING
            self._cancel_response_watchdog("bot_playback_started")
            self._cancel_caller_inactivity_timer("bot_playback_started")

        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_audio_playing = False
            self._metadata["bot_audio_playing"] = False
            self._maybe_arm_caller_inactivity_timer()

        elif direction is FrameDirection.DOWNSTREAM and isinstance(
            frame, LLMFullResponseStartFrame
        ):
            self._gemini_is_generating = True
            self._metadata["gemini_is_generating"] = True
            self._turn_state = ConversationTurnState.BOT_GENERATING
            self._cancel_response_watchdog("gemini_response_started")
            self._cancel_caller_inactivity_timer("gemini_response_started")

        elif direction is FrameDirection.DOWNSTREAM and isinstance(frame, LLMFullResponseEndFrame):
            self._gemini_is_generating = False
            self._metadata["gemini_is_generating"] = False
            logger.info("gemini_generation_completed", extra=self._log_context())
            self._maybe_arm_caller_inactivity_timer()

        elif direction is FrameDirection.DOWNSTREAM and isinstance(frame, OutputAudioRawFrame):
            self._gemini_is_generating = True
            self._metadata["gemini_is_generating"] = True
            self._response_audio_seen = True
            self._cancel_response_watchdog("gemini_audio_started")
            self._cancel_caller_inactivity_timer("gemini_audio_started")

        elif direction is FrameDirection.DOWNSTREAM and isinstance(frame, InterruptionFrame):
            self._gemini_is_generating = False
            self._metadata["gemini_is_generating"] = False
            self._cancel_response_watchdog("gemini_response_interrupted")
            logger.info(
                "conversation_interruption_frame",
                extra={**self._log_context(), "direction": direction.name.lower()},
            )
            self._maybe_arm_caller_inactivity_timer()

        # The initial greeting is the only non-caller turn armed here.
        elif (
            direction is FrameDirection.DOWNSTREAM
            and isinstance(frame, (LLMContextFrame, LLMRunFrame))
            and self._turn_id == 0
            and not self._caller_is_speaking
            and not self._gemini_is_generating
        ):
            if not is_call_terminating:
                self._arm_response_watchdog("initial_greeting")

        await self.push_frame(frame, direction)

    def _log_context(self) -> dict[str, Any]:
        return {
            "session_id": self._session.session_id,
            "provider": self._session.provider,
            "turn_id": self._turn_id,
            "call_state": self._call_state.value,
            "turn_state": self._turn_state.value,
            "caller_is_speaking": self._caller_is_speaking,
            "gemini_is_generating": self._gemini_is_generating,
            "bot_audio_playing": self._bot_audio_playing,
            "inactivity_stage": self._inactivity_stage,
        }

    def _arm_response_watchdog(self, trigger: str) -> None:
        if (
            self._closed
            or self._call_state != CallLifecycleState.ACTIVE
            or self._metadata.get("call_terminating")
            or self._caller_is_speaking
            or self._gemini_is_generating
            or self._bot_audio_playing
        ):
            return
        self._turn_id += 1
        turn_id = self._turn_id
        self._response_audio_seen = False
        self._turn_state = ConversationTurnState.WAITING_FOR_RESPONSE
        self._cancel_response_watchdog("superseded_by_new_turn")
        self._cancel_caller_inactivity_timer("watchdog_armed")
        logger.info(
            "gemini_response_watchdog_armed", extra={**self._log_context(), "trigger": trigger}
        )
        self._watch_task = self.create_task(
            self._watch_response(turn_id, trigger), name=f"gemini_response_watchdog_{turn_id}"
        )

    def _cancel_response_watchdog(self, reason: str) -> None:
        task = self._watch_task
        self._watch_task = None
        if task is not None and not task.done():
            logger.info(
                "gemini_response_watchdog_cancelled",
                extra={**self._log_context(), "reason": reason},
            )
            task.cancel()

    def _maybe_arm_caller_inactivity_timer(self) -> None:
        if (
            not self._caller_is_speaking
            and not self._gemini_is_generating
            and not self._bot_audio_playing
        ):
            self._arm_caller_inactivity_timer()

    def _timeout_for_stage(self, stage: int) -> float:
        if stage == 0:
            return self._first_inactivity_timeout
        elif stage == 1:
            return self._second_inactivity_timeout
        else:
            return self._final_inactivity_timeout

    def _arm_caller_inactivity_timer(self) -> None:
        if (
            self._closed
            or self._call_state != CallLifecycleState.ACTIVE
            or self._metadata.get("call_terminating")
            or self._session.metadata.get("call_terminated")
            or self._caller_is_speaking
            or self._gemini_is_generating
            or self._bot_audio_playing
        ):
            return
        stage = self._inactivity_stage
        if stage >= 3:
            return
        timeout = self._timeout_for_stage(stage)
        if timeout <= 0:
            return
        self._cancel_caller_inactivity_timer("rearmed")
        turn_id = self._turn_id
        self._turn_state = ConversationTurnState.AWAITING_CALLER
        self._caller_inactivity_task = self.create_task(
            self._watch_caller_inactivity(turn_id, stage, timeout),
            name=f"caller_inactivity_{turn_id}_stage_{stage}",
        )

    def _cancel_caller_inactivity_timer(self, reason: str) -> None:
        task = self._caller_inactivity_task
        self._caller_inactivity_task = None
        if task is not None and not task.done():
            logger.info(
                "caller_inactivity_timer_cancelled",
                extra={**self._log_context(), "reason": reason},
            )
            task.cancel()

    async def _send_inactivity_prompt(self, text: str) -> bool:
        """Send a prompt to the model requesting a silence follow-up message."""
        if callable(getattr(self._service, "prompt_caller_inactivity", None)):
            return await self._service.prompt_caller_inactivity(text)
        session_obj = getattr(self._service, "_session", None)
        if session_obj and hasattr(session_obj, "send_realtime_input"):
            try:
                await session_obj.send_realtime_input(text=text)
                return True
            except Exception as exc:
                logger.warning("direct_inactivity_prompt_failed", extra={"error": str(exc)})
                return False
        return False

    async def _terminate_inactivity_policy(self) -> None:
        """Terminate gracefully after completing the multi-stage inactivity policy."""
        self._call_state = CallLifecycleState.TERMINATING
        self._metadata["call_terminating"] = True
        self._cancel_response_watchdog("inactivity_policy_terminated")
        self._cancel_caller_inactivity_timer("inactivity_policy_terminated")
        goodbye_prompt = (
            self._metadata.get("inactivity_goodbye_prompt")
            or settings.caller_inactivity_goodbye_prompt
        )
        await self._send_inactivity_prompt(goodbye_prompt)
        callback = self._metadata.get("hang_up_call")
        if callable(callback):
            try:
                await callback()
            except Exception as exc:
                logger.error("inactivity_hangup_failed", extra={"error": str(exc)})

    async def _watch_caller_inactivity(self, turn_id: int, stage: int, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
            if (
                self._closed
                or self._call_state != CallLifecycleState.ACTIVE
                or turn_id != self._turn_id
                or self._caller_is_speaking
                or self._gemini_is_generating
                or self._bot_audio_playing
            ):
                return

            self._diagnostics.record_conversation_event("caller_inactivity_timeout")
            logger.info(
                "caller_inactivity_timeout",
                extra={**self._log_context(), "inactivity_stage": stage},
            )

            if stage == 0:
                self._diagnostics.record_conversation_event("caller_inactivity_first_prompt")
                self._inactivity_stage = 1
                logger.info("caller_inactivity_first_followup_triggered", extra=self._log_context())
                prompt_text = (
                    self._metadata.get("inactivity_first_prompt")
                    or settings.caller_inactivity_first_prompt
                )
                await self._send_inactivity_prompt(prompt_text)

            elif stage == 1:
                self._diagnostics.record_conversation_event("caller_inactivity_second_prompt")
                self._inactivity_stage = 2
                logger.info(
                    "caller_inactivity_second_followup_triggered", extra=self._log_context()
                )
                prompt_text = (
                    self._metadata.get("inactivity_second_prompt")
                    or settings.caller_inactivity_second_prompt
                )
                await self._send_inactivity_prompt(prompt_text)

            elif stage >= 2:
                self._diagnostics.record_conversation_event("caller_inactivity_policy_terminated")
                self._inactivity_stage = 3
                logger.info(
                    "caller_inactivity_policy_completed_terminating", extra=self._log_context()
                )
                await self._terminate_inactivity_policy()

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("caller_inactivity_watch_failed", extra={"error": str(exc)})

    async def _watch_response(self, turn_id: int, trigger: str) -> None:
        try:
            await asyncio.sleep(self._response_timeout)
            if (
                self._closed
                or self._call_state != CallLifecycleState.ACTIVE
                or turn_id != self._turn_id
                or self._caller_is_speaking
                or self._gemini_is_generating
                or self._bot_audio_playing
                or self._response_audio_seen
            ):
                return

            self._diagnostics.record_conversation_event("response_watchdog_fired")
            service_ready = bool(
                getattr(self._service, "_session", None)
                and getattr(self._service, "_ready_for_realtime_input", False)
            )
            logger.warning(
                "gemini_response_watchdog_fired",
                extra={
                    **self._log_context(),
                    "trigger": trigger,
                    "connection_ready": service_ready,
                    "timeout_seconds": self._response_timeout,
                },
            )

            # Re-check state after acquiring the lock. A caller may have begun
            # speaking while this task was waiting to recover.
            async with self._recovery_lock:
                if (
                    self._closed
                    or self._call_state != CallLifecycleState.ACTIVE
                    or turn_id != self._turn_id
                    or self._caller_is_speaking
                    or self._gemini_is_generating
                    or self._bot_audio_playing
                    or self._recovery_attempted_turn == turn_id
                ):
                    return
                self._recovery_attempted_turn = turn_id
                service_ready = bool(
                    getattr(self._service, "_session", None)
                    and getattr(self._service, "_ready_for_realtime_input", False)
                )
                if not service_ready:
                    logger.warning(
                        "gemini_watchdog_recovery_requires_reconnect",
                        extra=self._log_context(),
                    )
                recovered = await self._service.recover_unanswered_turn()

            if recovered:
                logger.info("gemini_watchdog_recovery_requested", extra=self._log_context())
                await asyncio.sleep(self._recovery_grace)
            if (
                self._closed
                or turn_id != self._turn_id
                or self._caller_is_speaking
                or self._gemini_is_generating
                or self._bot_audio_playing
                or self._metadata.get("call_terminating")
                or self._metadata.get("call_terminated")
            ):
                return
            logger.warning(
                "gemini_watchdog_recovery_finished_without_response",
                extra={**self._log_context(), "recovery_succeeded": recovered},
            )
            # A recovery nudge that produces no audio must not leave the caller
            # connected to a silent agent. The service fallback plays a short
            # prerecorded notice and requests hang-up through the provider's
            # normal audio-drain path, so the notice is heard before disconnect.
            fallback = getattr(self._service, "play_fallback_and_hangup", None)
            if callable(fallback):
                # The fallback emits response frames through this processor.
                # Detach the current task first so those frames do not cancel
                # the watchdog while it is performing the fallback itself.
                if self._watch_task is asyncio.current_task():
                    self._watch_task = None
                logger.warning(
                    "gemini_watchdog_fallback_started", extra=self._log_context()
                )
                fallback_completed = await fallback()
                logger.info(
                    "gemini_watchdog_fallback_finished",
                    extra={
                        **self._log_context(),
                        "fallback_completed": bool(fallback_completed),
                    },
                )
            else:
                logger.error(
                    "gemini_watchdog_fallback_unavailable", extra=self._log_context()
                )
        except asyncio.CancelledError:
            logger.info("gemini_response_watchdog_task_cancelled", extra=self._log_context())
            raise
        except Exception as error:
            logger.exception(
                "gemini_response_watchdog_failed",
                extra={**self._log_context(), "error_type": type(error).__name__},
            )

    async def cleanup(self) -> None:
        self._closed = True
        self._call_state = CallLifecycleState.TERMINATED
        self._metadata["call_terminated"] = True
        tasks = [
            task for task in (self._watch_task, self._caller_inactivity_task) if task is not None
        ]
        self._watch_task = None
        self._caller_inactivity_task = None
        for task in tasks:
            if not task.done():
                await self.cancel_task(task, timeout=1.0)
        await super().cleanup()
