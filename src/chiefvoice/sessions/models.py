from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


@dataclass(slots=True)
class AcceptedSession:
    channel: str
    provider: str
    direction: Direction
    caller: str | None = None
    callee: str | None = None
    provider_call_id: str | None = None
    tenant_id: str | None = None
    api_key_id: str | None = None
    bot_config_id: str | None = None
    live_agent_instructions: str | None = None
    live_agent_template_id: str | None = None
    live_agent_template_revision: int | None = None
    live_agent_context: dict[str, Any] | None = None
    live_agent_language: str | None = None
    live_agent_completion_requirements: dict[str, Any] | None = None
    post_call_context: str | None = None
    post_call_template_id: str | None = None
    post_call_template_revision: int | None = None
    post_call_failure_webhook_url: str | None = None
    post_call_failure_webhook_payload: dict[str, Any] | None = None
    post_call_output_schema: dict[str, Any] | None = None
    post_call_tools: list[str] | None = None
    post_call_time_zone: str | None = None
    post_call_time_format: str | None = None
    include_live_agent_context: bool = False
    post_call_agent_enabled: bool = True
    max_call_seconds: int | None = None
    recording_allowed: bool = False
    auto_hangup_when_complete: bool = False
    session_id: str = field(default_factory=lambda: str(uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)
