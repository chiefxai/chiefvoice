import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from chiefvoice.observability.setup import data_lifecycle_run

logger = logging.getLogger(__name__)


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


class StaticRetentionPolicyDirectory:
    """Explicit environment-backed policies for small deployments and development.

    This is configuration, not a tenant-managed persistent policy database. The JSON
    value must be supplied explicitly; an empty value means retention is disabled.
    """

    def __init__(self, policies: list[RetentionPolicy]):
        self._policies = tuple(policies)

    @property
    def policies(self) -> tuple[RetentionPolicy, ...]:
        return self._policies

    @classmethod
    def from_json(cls, value: str) -> "StaticRetentionPolicyDirectory":
        try:
            raw = json.loads(value or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("Retention policy configuration must be valid JSON") from exc
        if not isinstance(raw, list) or len(raw) > 1_000:
            raise ValueError("Retention policy configuration must be a list of at most 1000 items")
        policies: list[RetentionPolicy] = []
        seen: set[tuple[str, RetentionClass]] = set()
        for item in raw:
            if not isinstance(item, dict) or set(item) - {
                "tenant_id",
                "data_class",
                "retain_for_seconds",
                "legal_hold",
                "enabled",
            }:
                raise ValueError("Retention policy item has unsupported fields")
            tenant_id = item.get("tenant_id")
            seconds = item.get("retain_for_seconds")
            legal_hold = item.get("legal_hold", False)
            enabled = item.get("enabled", True)
            if (
                not isinstance(tenant_id, str)
                or not tenant_id.strip()
                or type(seconds) is not int
                or type(legal_hold) is not bool
                or type(enabled) is not bool
            ):
                raise ValueError("Retention policy item is invalid")
            try:
                policy = RetentionPolicy(
                    tenant_id=tenant_id,
                    data_class=RetentionClass(item["data_class"]),
                    retain_for=timedelta(seconds=seconds),
                    legal_hold=legal_hold,
                    enabled=enabled,
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Retention policy item is invalid") from exc
            policy_key = (policy.tenant_id, policy.data_class)
            if policy_key in seen:
                raise ValueError("Retention policies must be unique per tenant and data class")
            seen.add(policy_key)
            policies.append(policy)
        return cls(policies)

    async def list_policies(self) -> list[RetentionPolicy]:
        return list(self._policies)


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


async def run_retention_periodically(
    runner: RetentionRunner, interval_seconds: int
) -> None:
    """Run the bounded retention pass at a fixed interval until cancelled."""
    if interval_seconds < 60:
        raise ValueError("Retention interval must be at least 60 seconds")
    while True:
        try:
            deleted = await runner.run_once(datetime.now(UTC))
            logger.info(
                "retention_run_completed",
                extra={"deleted_count": sum(deleted.values())},
            )
            data_lifecycle_run("retention", "succeeded")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            data_lifecycle_run("retention", "failed")
            logger.error(
                "retention_run_failed",
                extra={"error_type": type(exc).__name__},
            )
        await asyncio.sleep(interval_seconds)
