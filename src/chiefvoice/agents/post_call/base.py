from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class CompletedCall:
    """Provider-neutral call data made available to asynchronous post-call work."""

    session_id: str
    tenant_id: str
    provider: str
    channel: str
    direction: str
    live_agent_language: str | None = None
    api_key_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    transcript: str | None = None
    recording_uri: str | None = None
    recording_audio: bytes | None = None
    recording_loader: Callable[[], Awaitable[bytes | None]] | None = None
    post_call_context: str | None = None
    output_schema: dict[str, Any] | None = None
    tools: tuple[str, ...] = ()
    time_zone: str = "UTC"
    time_format: str = "%Y-%m-%dT%H:%M:%S%z"
    live_agent_context: dict[str, Any] | None = None
    live_call_claim: dict[str, Any] | None = None
    identity_tool_events: tuple[dict[str, Any], ...] = ()
    expected_assignment: dict[str, Any] | None = None
    audio_review_transcript: str | None = None
    audio_review_attempted: bool = False
    failure_webhook_url: str | None = None
    failure_webhook_payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PostCallResult:
    """Structured result from summarization, classification, or extraction."""

    summary: str | None = None
    disposition: str | None = None
    extracted_data: dict[str, Any] = field(default_factory=dict)
    action_items: tuple[str, ...] = ()
    structured_output: dict[str, Any] | None = None
    transcript_status: Literal["clear", "unclear", "no_data"] = "clear"
    needs_audio_review: bool = False
    attendance_validation: dict[str, Any] | None = None


class PostCallAgent(Protocol):
    """Separate interface for asynchronous post-call analysis providers."""

    name: str

    async def process(self, call: CompletedCall) -> PostCallResult: ...
