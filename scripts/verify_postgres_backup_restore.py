"""Restore the latest encrypted service backup into a disposable database.

Set RESTORE_TARGET_DATABASE_URL to a newly created empty database on the PostgreSQL
instance. This intentionally restores real production contents, so run it only in an
isolated, access-controlled environment and drop the scratch database afterwards.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

from chiefvoice.core.config import settings
from chiefvoice.data_lifecycle.backup import BackupPolicy, BackupScope
from chiefvoice.data_lifecycle.postgres_backup import EncryptedPostgresBackupProvider
from chiefvoice.storage.client import create_object_storage_client


async def main() -> None:
    target = os.environ.get("RESTORE_TARGET_DATABASE_URL", "")
    if not target:
        raise SystemExit("RESTORE_TARGET_DATABASE_URL must point to a disposable empty database")
    if not settings.database_url or not settings.backup_encryption_key or not settings.object_storage_bucket:
        raise SystemExit("DATABASE_URL, BACKUP_ENCRYPTION_KEY and OBJECT_STORAGE_BUCKET are required")

    client = create_object_storage_client()
    provider = EncryptedPostgresBackupProvider(
        settings.database_url,
        settings.backup_directory,
        settings.backup_encryption_key,
        allowed_restore_targets=(target,),
        s3_client=client,
        s3_bucket=settings.object_storage_bucket,
        s3_prefix="backups",
    )
    policy = BackupPolicy(
        scope=BackupScope.SERVICE,
        tenant_id=None,
        resource="postgres",
        interval=timedelta(days=1),
        retain_for=timedelta(days=30),
    )
    try:
        artifacts = await provider.list_backups(policy)
        if not artifacts:
            raise SystemExit("No service backups were found in object storage")
        artifact = max(artifacts, key=lambda item: item.created_at)
        if not await provider.verify_backup(artifact.backup_id):
            raise SystemExit(f"Backup archive verification failed: {artifact.backup_id}")
        await provider.restore_backup(artifact.backup_id, target)
        print(f"restore_verified backup_id={artifact.backup_id} created_at={artifact.created_at.isoformat()}")
    finally:
        await asyncio.to_thread(client.close)


if __name__ == "__main__":
    asyncio.run(main())
