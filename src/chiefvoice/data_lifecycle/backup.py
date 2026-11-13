import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from chiefvoice.observability.setup import data_lifecycle_run

logger = logging.getLogger(__name__)


class BackupScope(StrEnum):
    TENANT = "tenant"
    SERVICE = "service"


@dataclass(frozen=True, slots=True)
class BackupPolicy:
    scope: BackupScope
    tenant_id: str | None
    resource: str
    interval: timedelta
    retain_for: timedelta
    encryption_required: bool = True

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0) or self.retain_for <= timedelta(0):
            raise ValueError("Backup interval and retention must be positive")
        if self.scope is BackupScope.TENANT and not self.tenant_id:
            raise ValueError("Tenant-scoped backup policy requires tenant_id")
        if self.scope is BackupScope.SERVICE and self.tenant_id is not None:
            raise ValueError("Service-scoped backup policy cannot specify tenant_id")


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    backup_id: str
    scope: BackupScope
    tenant_id: str | None
    resource: str
    created_at: datetime
    storage_uri: str
    checksum_sha256: str
    encrypted: bool
    size_bytes: int


class BackupProvider(Protocol):
    """Backend-neutral snapshot, verification, restore, and expiry interface."""

    async def create_backup(self, policy: BackupPolicy) -> BackupArtifact: ...

    async def get_backup(self, backup_id: str) -> BackupArtifact | None: ...

    async def list_backups(self, policy: BackupPolicy) -> list[BackupArtifact]: ...

    async def verify_backup(self, backup_id: str) -> bool: ...

    async def restore_backup(self, backup_id: str, target: str) -> None: ...

    async def delete_backup(self, backup_id: str) -> None: ...


class BackupPolicyDirectory(Protocol):
    async def list_policies(self) -> list[BackupPolicy]: ...


class StaticBackupPolicyDirectory:
    """Explicit environment-backed service backup policies; empty config disables backups."""

    def __init__(self, policies: list[BackupPolicy]):
        self._policies = tuple(policies)

    @property
    def policies(self) -> tuple[BackupPolicy, ...]:
        return self._policies

    @classmethod
    def from_json(cls, value: str) -> "StaticBackupPolicyDirectory":
        try:
            raw = json.loads(value or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("Backup policy configuration must be valid JSON") from exc
        if not isinstance(raw, list) or len(raw) > 100:
            raise ValueError("Backup policy configuration must be a list of at most 100 items")
        policies: list[BackupPolicy] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict) or set(item) - {
                "scope",
                "tenant_id",
                "resource",
                "interval_seconds",
                "retain_for_seconds",
                "encryption_required",
            }:
                raise ValueError("Backup policy item has unsupported fields")
            if item.get("scope") != BackupScope.SERVICE.value:
                raise ValueError("Only service-scoped database backups are supported")
            if item.get("tenant_id") is not None:
                raise ValueError("Service-scoped backup policy cannot specify tenant_id")
            resource = item.get("resource")
            interval = item.get("interval_seconds")
            retention = item.get("retain_for_seconds")
            encryption = item.get("encryption_required", True)
            if (
                not isinstance(resource, str)
                or not resource.strip()
                or type(interval) is not int
                or type(retention) is not int
                or type(encryption) is not bool
            ):
                raise ValueError("Backup policy item is invalid")
            policy = BackupPolicy(
                scope=BackupScope.SERVICE,
                tenant_id=None,
                resource=resource.strip(),
                interval=timedelta(seconds=interval),
                retain_for=timedelta(seconds=retention),
                encryption_required=encryption,
            )
            if policy.resource in seen:
                raise ValueError("Backup policies must be unique per service resource")
            seen.add(policy.resource)
            policies.append(policy)
        return cls(policies)

    async def list_policies(self) -> list[BackupPolicy]:
        return list(self._policies)


class BackupRunner:
    def __init__(self, policies: BackupPolicyDirectory, provider: BackupProvider):
        self.policies = policies
        self.provider = provider

    async def create_and_verify(self, policy: BackupPolicy) -> BackupArtifact:
        artifact = await self.provider.create_backup(policy)
        if policy.encryption_required and not artifact.encrypted:
            await self.provider.delete_backup(artifact.backup_id)
            raise RuntimeError("Backup provider returned an unencrypted artifact")
        if not await self.provider.verify_backup(artifact.backup_id):
            await self.provider.delete_backup(artifact.backup_id)
            raise RuntimeError("Backup verification failed")
        return artifact

    async def run_once(self, now: datetime) -> list[BackupArtifact]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Backup run time must be timezone-aware")
        artifacts = []
        for policy in await self.policies.list_policies():
            existing_backups = await self.provider.list_backups(policy)
            latest = max((item.created_at for item in existing_backups), default=None)
            if latest is None or latest <= now - policy.interval:
                created = await self.create_and_verify(policy)
                artifacts.append(created)

            cutoff = now - policy.retain_for
            for existing in existing_backups:
                if existing.created_at < cutoff:
                    await self.provider.delete_backup(existing.backup_id)
        return artifacts


async def run_backups_periodically(runner: BackupRunner, interval_seconds: int) -> None:
    """Run backup/verification/retention passes until the task is cancelled."""
    if interval_seconds < 60:
        raise ValueError("Backup runner interval must be at least 60 seconds")
    while True:
        try:
            created = await runner.run_once(datetime.now(UTC))
            logger.info("backup_run_completed", extra={"created_count": len(created)})
            data_lifecycle_run("backup", "succeeded")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            data_lifecycle_run("backup", "failed")
            logger.error("backup_run_failed", extra={"error_type": type(exc).__name__})
        await asyncio.sleep(interval_seconds)
