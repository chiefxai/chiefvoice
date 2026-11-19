"""Playback receipts for Vobiz bidirectional audio streams."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from uuid import uuid4

from chiefvoice.agents.call.audio_diagnostics import CallAudioDiagnostics

logger = logging.getLogger(__name__)


class VobizPlaybackTracker:
    """Send ordered Vobiz checkpoints and correlate their playback receipts."""

    def __init__(self, session_id: str, diagnostics: CallAudioDiagnostics):
        self._session_id = session_id
        self._diagnostics = diagnostics
        self._checkpoints: dict[str, dict[str, object]] = {}
        self._latest_checkpoint: str | None = None

    async def send_checkpoint(self, websocket, stream_id: str) -> str:
        name = f"cv-{uuid4().hex}"
        checkpoint: dict[str, object] = {
            "event": asyncio.Event(),
            "sent_at": time.monotonic(),
            "result": "pending",
        }
        # Only the latest speech turn gates a possible hang-up; discard older
        # receipts so long calls do not grow this map without bound.
        self._checkpoints.clear()
        self._checkpoints[name] = checkpoint
        self._latest_checkpoint = name
        try:
            await websocket.send_text(
                json.dumps(
                    {"event": "checkpoint", "streamId": stream_id, "name": name},
                    separators=(",", ":"),
                )
            )
        except Exception:
            checkpoint["result"] = "send_failed"
            cast_event(checkpoint["event"]).set()
            self._diagnostics.record_conversation_event("vobiz_playback_checkpoint_send_failed")
            logger.warning(
                "vobiz_playback_checkpoint_send_failed",
                extra={"session_id": self._session_id},
                exc_info=True,
            )
            return name

        self._diagnostics.record_conversation_event("vobiz_playback_checkpoint_sent")
        logger.info(
            "vobiz_playback_checkpoint_sent",
            extra={"session_id": self._session_id, "checkpoint": name},
        )
        return name

    def acknowledge(self, name: str | None) -> bool:
        checkpoint = self._checkpoints.get(name or "")
        if checkpoint is None or checkpoint["result"] != "pending":
            return False
        checkpoint["result"] = "played"
        cast_event(checkpoint["event"]).set()
        latency_ms = round((time.monotonic() - float(checkpoint["sent_at"])) * 1000)
        self._diagnostics.record_conversation_event("vobiz_playback_ack_received")
        logger.info(
            "vobiz_playback_ack_received",
            extra={
                "session_id": self._session_id,
                "checkpoint": name,
                "latency_ms": latency_ms,
            },
        )
        return True

    def mark_interrupted(self) -> None:
        checkpoint = self._checkpoints.get(self._latest_checkpoint or "")
        if checkpoint is None or checkpoint["result"] != "pending":
            return
        checkpoint["result"] = "interrupted"
        cast_event(checkpoint["event"]).set()
        self._diagnostics.record_conversation_event("vobiz_playback_interrupted")
        logger.info("vobiz_playback_interrupted", extra={"session_id": self._session_id})

    async def wait_for_latest_ack(
        self, timeout_seconds: float, cancel_event: asyncio.Event | None = None
    ) -> str:
        """Wait for the latest utterance receipt, caller activity, or timeout."""
        name = self._latest_checkpoint
        if name is None:
            return "no_checkpoint"
        checkpoint = self._checkpoints[name]
        event = cast_event(checkpoint["event"])
        if event.is_set():
            return str(checkpoint["result"])

        ack_task = asyncio.create_task(event.wait())
        cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        waiting = {ack_task}
        if cancel_task is not None:
            waiting.add(cancel_task)
        try:
            done, _ = await asyncio.wait(
                waiting,
                timeout=max(0.0, timeout_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task is not None and cancel_task in done and cancel_event.is_set():
                return "caller_activity"
            if ack_task in done:
                return str(checkpoint["result"])
            checkpoint["result"] = "timeout"
            event.set()
            self._diagnostics.record_conversation_event("vobiz_playback_ack_timeout")
            logger.warning(
                "vobiz_playback_ack_timeout",
                extra={"session_id": self._session_id, "checkpoint": name},
            )
            return "timeout"
        finally:
            for task in (ack_task, cancel_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (ack_task, cancel_task) if task is not None),
                return_exceptions=True,
            )


def cast_event(value: object) -> asyncio.Event:
    """Keep the private checkpoint record compact while checking its event type."""
    if not isinstance(value, asyncio.Event):
        raise TypeError("Vobiz checkpoint event is invalid")
    return value
