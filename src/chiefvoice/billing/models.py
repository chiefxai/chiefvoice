from dataclasses import dataclass
from enum import StrEnum


class VoiceCapacityFull(Exception):
    """A valid call could not reserve a slot because all concurrent slots are busy."""


class BillingMode(StrEnum):
    """Billing arrangement selected when a tenant account is created."""

    PREPAID = "prepaid"
    POSTPAID = "postpaid"


@dataclass(frozen=True, slots=True)
class VoiceEntitlement:
    tenant_id: str
    billing_mode: BillingMode
    can_use_voice: bool
    allowed_channels: frozenset[str]
    max_concurrent_sessions: int
    recording_allowed: bool
    max_call_seconds: int = 3_600
