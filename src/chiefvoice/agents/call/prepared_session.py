"""In-process handoff for a realtime model connection prepared before dialing."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pipecat.processors.frame_processor import FrameProcessor

from chiefvoice.sessions import AcceptedSession

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PreparedRealtimeSession:
    processors: Sequence[FrameProcessor]
    task_manager: object
    runtime_metadata: dict[str, Any]
    created_at: float
    cleanup_lock: asyncio.Lock
    consumed: bool = False
    expiry_task: asyncio.Task | None = None

    def bind_runtime_metadata(self, live_session: AcceptedSession) -> None:
        """Share call-time state with processors created during preflight.

        Vobiz reloads an AcceptedSession from encrypted storage when media
        connects. The preflight Gemini tool callbacks still close over the
        original object, so merge the live callback state and then make both
        session objects use the same mutable metadata dictionary.
        """
        self.runtime_metadata.update(live_session.metadata)
        live_session.metadata = self.runtime_metadata


class PreparedRealtimeSessionDirectory:
    """Keeps one already-connected realtime service per outbound call."""

    def __init__(self, ttl_seconds: float = 180.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._sessions: dict[str, PreparedRealtimeSession] = {}
        self._lock = asyncio.Lock()

    async def put(
        self,
        session_id: str,
        processors: Sequence[FrameProcessor],
        task_manager: object,
        runtime_metadata: dict[str, Any],
    ) -> None:
        entry = PreparedRealtimeSession(
            processors=processors,
            task_manager=task_manager,
            runtime_metadata=runtime_metadata,
            created_at=time.monotonic(),
            cleanup_lock=asyncio.Lock(),
        )
        async with self._lock:
            previous = self._sessions.pop(session_id, None)
            self._sessions[session_id] = entry
        if previous is not None:
            await self._cleanup(session_id, previous, "replaced")
        entry.expiry_task = asyncio.create_task(
            self._expire(session_id, entry), name=f"prepared-session-{session_id}"
        )

    async def take(self, session_id: str) -> PreparedRealtimeSession | None:
        async with self._lock:
            entry = self._sessions.pop(session_id, None)
        if entry is None:
            return None
        age = time.monotonic() - entry.created_at
        if age > self._ttl_seconds:
            await self._cleanup(session_id, entry, "expired_before_media")
            return None
        self._cancel_expiry(entry)
        entry.consumed = True
        logger.info(
            "gemini_prepared_session_consumed",
            extra={"session_id": session_id, "age_ms": round(age * 1000)},
        )
        return entry

    async def discard(self, session_id: str, reason: str) -> None:
        async with self._lock:
            entry = self._sessions.pop(session_id, None)
        if entry is not None:
            self._cancel_expiry(entry)
            await self._cleanup(session_id, entry, reason)

    async def close_all(self, reason: str = "application_shutdown") -> None:
        async with self._lock:
            entries = list(self._sessions.items())
            self._sessions.clear()
        for session_id, entry in entries:
            self._cancel_expiry(entry)
            await self._cleanup(session_id, entry, reason)

    @staticmethod
    def _cancel_expiry(entry: PreparedRealtimeSession) -> None:
        task = entry.expiry_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _expire(self, session_id: str, entry: PreparedRealtimeSession) -> None:
        try:
            await asyncio.sleep(self._ttl_seconds)
            async with self._lock:
                if self._sessions.get(session_id) is not entry:
                    return
                self._sessions.pop(session_id, None)
            await self._cleanup(session_id, entry, "ttl_expired")
        except asyncio.CancelledError:
            raise

    async def _cleanup(
        self, session_id: str, entry: PreparedRealtimeSession, reason: str
    ) -> None:
        async with entry.cleanup_lock:
            for processor in reversed(entry.processors):
                try:
                    await processor.cleanup()
                except Exception as exc:
                    logger.warning(
                        "gemini_prepared_session_cleanup_failed",
                        extra={
                            "session_id": session_id,
                            "reason": reason,
                            "error_type": type(exc).__name__,
                        },
                    )
            logger.info(
                "gemini_prepared_session_closed",
                extra={"session_id": session_id, "reason": reason},
            )
