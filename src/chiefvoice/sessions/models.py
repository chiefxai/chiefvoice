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
    bot_config_id: str | None = None
    recording_allowed: bool = False
    session_id: str = field(default_factory=lambda: str(uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)
