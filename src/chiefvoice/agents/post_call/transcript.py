import inspect
import logging
from dataclasses import dataclass, field
from typing import Any

from pipecat.frames.frames import Frame, TranscriptionFrame, TTSTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from chiefvoice.sessions import AcceptedSession

MAX_TRANSCRIPT_CHARACTERS = 1_000_000
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _TranscriptBuffer:
    segments: list[str] = field(default_factory=list)
    characters: int = 0


class TranscriptCollector(FrameProcessor):
    """Collect Gemini Live user and assistant text without logging it."""

    def __init__(
        self,
        session: AcceptedSession,
        *,
        capture_user: bool = True,
        capture_assistant: bool = True,
        buffer: _TranscriptBuffer | None = None,
    ):
        super().__init__()
        self._session = session
        self._capture_user = capture_user
        self._capture_assistant = capture_assistant
        self._buffer = buffer or _TranscriptBuffer()

    @classmethod
    def for_pipeline(
        cls, session: AcceptedSession
    ) -> tuple["TranscriptCollector", "TranscriptCollector"]:
        """Place separate taps on both sides of a realtime model in a Pipecat pipeline.

        Caller transcription frames travel upstream from the model, while assistant text
        frames travel downstream. A single processor before the model cannot observe both.
        """
        buffer = _TranscriptBuffer()
        return (
            cls(session, capture_user=True, capture_assistant=False, buffer=buffer),
            cls(session, capture_user=False, capture_assistant=True, buffer=buffer),
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        text = None
        speaker = None
        if (
            self._capture_user
            and isinstance(frame, TranscriptionFrame)
            and direction is FrameDirection.UPSTREAM
        ):
            text, speaker = frame.text, "Caller"
        elif (
            self._capture_assistant
            and isinstance(frame, TTSTextFrame)
            and direction is FrameDirection.DOWNSTREAM
        ):
            text, speaker = frame.text, "Assistant"

        if text and speaker and self._buffer.characters < MAX_TRANSCRIPT_CHARACTERS:
            remaining = MAX_TRANSCRIPT_CHARACTERS - self._buffer.characters
            text = text.strip()[:remaining]
            if text:
                if speaker == "Caller":
                    logger.info(
                        "caller_response_received",
                        extra={
                            "session_id": self._session.session_id,
                            "characters": len(text),
                        },
                    )
                self._buffer.segments.append(f"{speaker}: {text}")
                self._buffer.characters += len(text)
                if len(self._buffer.segments) == 1:
                    logger.info(
                        "call_transcript_first_segment",
                        extra={
                            "session_id": self._session.session_id,
                            "speaker": speaker.lower(),
                            "characters": len(text),
                        },
                    )
                self._session.metadata["post_call_transcript"] = "\n".join(
                    self._buffer.segments
                )
                await self._persist_segment(speaker, text)

        await self.push_frame(frame, direction)

    async def _persist_segment(self, speaker: str, text: str) -> None:
        callback: Any = self._session.metadata.get("persist_transcript_segment")
        if not callable(callback):
            return
        try:
            result = callback(speaker, text)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            # Persistence must not break the live audio pipeline. Mark the session so
            # call finalization can report/reconcile the incomplete durable transcript.
            self._session.metadata["transcript_persistence_error"] = True
            logger.error(
                "call_transcript_segment_persistence_failed",
                extra={
                    "session_id": self._session.session_id,
                    "tenant_id": self._session.tenant_id,
                    "error_type": type(exc).__name__,
                },
            )
