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
    enabled: bool
    created_at: datetime
    updated_at: datetime
