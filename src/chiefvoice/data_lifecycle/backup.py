from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol


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
