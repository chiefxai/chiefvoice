"""Provider and credential configuration for reusable customer agents."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

AgentKind = Literal["calling", "post_call"]


@dataclass(frozen=True, slots=True)
class AgentConfiguration:
    agent_id: str
    tenant_id: str
    name: str
    kind: AgentKind
    provider: str
    model: str
    location: str
    cost_per_minute_minor: int
    currency: str
    auto_hangup_when_complete: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime
    tags: tuple[str, ...] = ()
