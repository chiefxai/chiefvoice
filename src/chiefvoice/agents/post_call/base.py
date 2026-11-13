from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CompletedCall:
    """Provider-neutral call data made available to asynchronous post-call work."""

    session_id: str
    tenant_id: str
    provider: str
    channel: str
    direction: str
    started_at: str | None = None
    ended_at: str | None = None
    transcript: str | None = None
    recording_uri: str | None = None


@dataclass(frozen=True, slots=True)
class PostCallResult:
    """Structured result from summarization, classification, or extraction."""

    summary: str | None = None
    disposition: str | None = None
    extracted_data: dict[str, Any] = field(default_factory=dict)
    action_items: tuple[str, ...] = ()


class PostCallAgent(Protocol):
    """Separate interface for asynchronous post-call analysis providers."""

    name: str

    async def process(self, call: CompletedCall) -> PostCallResult: ...
