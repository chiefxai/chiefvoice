"""Generic contract for explicitly registered live-call tool extensions."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from pipecat.adapters.schemas.function_schema import FunctionSchema

from chiefvoice.sessions import AcceptedSession


class LiveCallToolExtension(Protocol):
    """Business capability that adds tools to a live call explicitly."""

    def register(
        self,
        session: AcceptedSession,
        capture_tool_execution: Callable[..., None],
    ) -> tuple[Sequence[FunctionSchema], str]:
        """Return registered tools and their instruction text for this session."""
        ...

    def configure(self, session: AcceptedSession) -> None:
        """Attach extension-owned validators/state required by the capability."""
        ...
