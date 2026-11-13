from pipecat.frames.frames import Frame, TranscriptionFrame, TTSTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from chiefvoice.sessions import AcceptedSession

MAX_TRANSCRIPT_CHARACTERS = 1_000_000


class TranscriptCollector(FrameProcessor):
    """Collect Gemini Live user and assistant text without logging it."""

    def __init__(self, session: AcceptedSession):
        super().__init__()
        self._session = session
        self._segments: list[str] = []
        self._characters = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        text = None
        speaker = None
        if isinstance(frame, TranscriptionFrame) and direction is FrameDirection.UPSTREAM:
            text, speaker = frame.text, "Caller"
        elif isinstance(frame, TTSTextFrame) and direction is FrameDirection.DOWNSTREAM:
            text, speaker = frame.text, "Assistant"

        if text and speaker and self._characters < MAX_TRANSCRIPT_CHARACTERS:
            remaining = MAX_TRANSCRIPT_CHARACTERS - self._characters
            text = text.strip()[:remaining]
            if text:
                self._segments.append(f"{speaker}: {text}")
                self._characters += len(text)
                self._session.metadata["post_call_transcript"] = "\n".join(self._segments)

        await self.push_frame(frame, direction)
