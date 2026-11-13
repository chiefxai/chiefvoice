from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol


class RetentionClass(StrEnum):
    CALL_METADATA = "call_metadata"
    TRANSCRIPT = "transcript"
    RECORDING = "recording"
    AUDIT_EVENT = "audit_event"
    PROVIDER_INTEGRATION = "provider_integration"


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    tenant_id: str
    data_class: RetentionClass
    retain_for: timedelta
    legal_hold: bool = False
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("Retention policy requires a tenant_id")
        if self.retain_for <= timedelta(0):
            raise ValueError("Retention duration must be positive")

    def cutoff(self, now: datetime) -> datetime:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Retention cutoff time must include a timezone")
        return now - self.retain_for


class RetentionPolicyDirectory(Protocol):
    async def list_policies(self) -> list[RetentionPolicy]: ...


class RetentionTarget(Protocol):
    """A data store that can purge expired rows/objects for one tenant and class."""

    data_class: RetentionClass

    async def purge_expired(
        self, tenant_id: str, cutoff: datetime, batch_size: int
    ) -> int:
        """Delete up to batch_size expired records, skipping legal-hold items."""
        ...


class RetentionRunner:
    def __init__(
        self,
        policy_directory: RetentionPolicyDirectory,
        targets: list[RetentionTarget],
        *,
        batch_size: int = 500,
    ):
        if batch_size < 1:
            raise ValueError("Retention batch size must be positive")
        self.policy_directory = policy_directory
        self.targets = {target.data_class: target for target in targets}
        if len(self.targets) != len(targets):
            raise ValueError("Only one retention target may be registered per data class")
        self.batch_size = batch_size

    async def run_once(self, now: datetime) -> dict[str, int]:
        deleted: dict[str, int] = {}
        for policy in await self.policy_directory.list_policies():
            if not policy.enabled or policy.legal_hold:
                continue
            target = self.targets.get(policy.data_class)
            if target is None:
                continue
            deleted_key = f"{policy.tenant_id}:{policy.data_class.value}"
            deleted[deleted_key] = await target.purge_expired(
                policy.tenant_id, policy.cutoff(now), self.batch_size
            )
        return deleted
