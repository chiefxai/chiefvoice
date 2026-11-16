"""Turn diagnostics and bounded recovery for Gemini Live phone conversations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pipecat.frames.frames import (
    InterruptionFrame,
    LLMContextFrame,
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


class GeminiConversationWatchdog(FrameProcessor):
    """Observe local VAD and model output; recover one silent Gemini turn."""

    def __init__(
        self,
        session: AcceptedSession,
        service: Any,
        diagnostics: CallAudioDiagnostics,
        *,
        response_timeout_seconds: float | None = None,
        recovery_grace_seconds: float | None = None,
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
        self._turn = 0
        self._response_audio_seen = False
        self._response_started = False
        self._watch_task: asyncio.Task | None = None
        self._closed = False

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame) and direction is FrameDirection.DOWNSTREAM:
            self._diagnostics.record_conversation_event("caller_speech_started")
            logger.info(
                "caller_speech_detected_by_local_vad",
                extra={"session_id": self._session.session_id, "provider": self._session.provider},
            )
        elif isinstance(frame, UserStoppedSpeakingFrame) and direction is FrameDirection.DOWNSTREAM:
            self._diagnostics.record_conversation_event("caller_speech_stopped")
            logger.info(
                "caller_speech_stopped_by_local_vad",
                extra={"session_id": self._session.session_id, "provider": self._session.provider},
            )
        elif isinstance(frame, InterruptionFrame):
            logger.info(
                "conversation_interruption_frame",
                extra={
                    "session_id": self._session.session_id,
                    "provider": self._session.provider,
                    "direction": direction.name.lower(),
                },
            )

        if (
            direction is FrameDirection.DOWNSTREAM
            and isinstance(frame, (LLMContextFrame, LLMRunFrame))
            and not self._response_audio_seen
        ):
            # The initial greeting is a model turn too; this timer catches a
            # connected call where Gemini never speaks before the caller does.
            self._arm_watchdog("initial_or_requested_turn")
        elif direction is FrameDirection.UPSTREAM and isinstance(frame, TranscriptionFrame):
            if frame.text.strip():
                self._arm_watchdog("caller_turn")
        elif direction is FrameDirection.DOWNSTREAM and isinstance(
            frame, LLMFullResponseStartFrame
        ):
            self._response_started = True
        elif direction is FrameDirection.DOWNSTREAM and isinstance(frame, OutputAudioRawFrame):
            self._response_audio_seen = True
            self._cancel_watchdog()
        await self.push_frame(frame, direction)

    def _arm_watchdog(self, trigger: str) -> None:
        if self._closed:
            return
        self._turn += 1
        turn = self._turn
        self._response_audio_seen = False
        self._response_started = False
        self._cancel_watchdog()
        self._watch_task = self.create_task(
            self._watch_turn(turn, trigger), name="gemini_response_watchdog"
        )

    def _cancel_watchdog(self) -> None:
        task = self._watch_task
        self._watch_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _watch_turn(self, turn: int, trigger: str) -> None:
        try:
            await asyncio.sleep(self._response_timeout)
            if self._closed or turn != self._turn or self._response_audio_seen:
                return

            service_ready = bool(
                getattr(self._service, "_session", None)
                and getattr(self._service, "_ready_for_realtime_input", False)
            )
            generation_active = bool(getattr(self._service, "_bot_is_responding", False))
            self._diagnostics.record_conversation_event("response_watchdog_fired")
            logger.error(
                "gemini_response_watchdog_fired",
                extra={
                    "session_id": self._session.session_id,
                    "provider": self._session.provider,
                    "trigger": trigger,
                    "connection_ready": service_ready,
                    "generation_active": generation_active,
                    "response_started": self._response_started,
                    "timeout_seconds": self._response_timeout,
                },
            )

            recovered = await self._service.recover_unanswered_turn()
            if recovered:
                await asyncio.sleep(self._recovery_grace)
            if self._closed or turn != self._turn or self._response_audio_seen:
                return

            logger.error(
                "gemini_response_watchdog_exhausted",
                extra={
                    "session_id": self._session.session_id,
                    "provider": self._session.provider,
                    "recovery_succeeded": recovered,
                    "connection_ready": bool(
                        getattr(self._service, "_session", None)
                        and getattr(self._service, "_ready_for_realtime_input", False)
                    ),
                    "generation_active": bool(
                        getattr(self._service, "_bot_is_responding", False)
                    ),
                },
            )
            await self._service.play_fallback_and_hangup()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception(
                "gemini_response_watchdog_failed",
                extra={
                    "session_id": self._session.session_id,
                    "provider": self._session.provider,
                    "error_type": type(error).__name__,
                },
            )

    async def cleanup(self) -> None:
        self._closed = True
        task = self._watch_task
        self._watch_task = None
        if task is not None and not task.done():
            await self.cancel_task(task, timeout=1.0)
        await super().cleanup()
