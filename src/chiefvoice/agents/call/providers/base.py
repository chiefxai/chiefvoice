from collections.abc import Awaitable, Sequence
from typing import Protocol

from pipecat.processors.frame_processor import FrameProcessor

from chiefvoice.sessions import AcceptedSession


class RealtimeAgentProvider(Protocol):
    """Provider boundary for a speech-to-speech realtime conversation engine."""

    name: str

    def create_processors(
        self, session: AcceptedSession
    ) -> Sequence[FrameProcessor] | Awaitable[Sequence[FrameProcessor]]: ...

    async def preflight(self, session: AcceptedSession) -> None:
        """Confirm provider connectivity before an outbound call is dialed."""
        ...
