import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from chiefvoice.data_lifecycle.backup import (
    BackupArtifact,
    BackupScope,
    StaticBackupPolicyDirectory,
)
from chiefvoice.data_lifecycle.postgres_backup import (
    EncryptedPostgresBackupProvider,
    _MAGIC,
    _artifact_payload,
    _write_private_file,
    _libpq_environment,
)


def test_policy_config_is_explicit_and_service_scoped():
    directory = StaticBackupPolicyDirectory.from_json(
        '[{"scope":"service","resource":"postgres","interval_seconds":86400,'
        '"retain_for_seconds":604800}]'
    )

    assert len(directory._policies) == 1
    assert directory._policies[0].scope is BackupScope.SERVICE
    assert directory._policies[0].tenant_id is None
    assert StaticBackupPolicyDirectory.from_json("[]")._policies == ()

    with pytest.raises(ValueError):
        StaticBackupPolicyDirectory.from_json(
            '[{"scope":"tenant","tenant_id":"tenant-a","resource":"postgres",'
            '"interval_seconds":86400,"retain_for_seconds":604800}]'
        )
    with pytest.raises(ValueError):
        StaticBackupPolicyDirectory.from_json(
            '[{"scope":"service","tenant_id":"tenant-a","resource":"postgres",'
            '"interval_seconds":86400,"retain_for_seconds":604800}]'
        )


def test_database_credentials_are_parsed_into_environment_not_url_arguments():
    environment, identity = _libpq_environment(
        "postgresql+asyncpg://user%40name:secret%2Fvalue@db.example:5433/chiefvoice?sslmode=require"
    )

    assert environment["PGUSER"] == "user@name"
    assert environment["PGPASSWORD"] == "secret/value"
    assert environment["PGSSLMODE"] == "require"
    assert identity == ("db.example", "5433", "chiefvoice")


def test_encrypted_backup_payload_round_trips_and_authenticates(tmp_path: Path):
    key = Fernet.generate_key().decode("ascii")
    provider = EncryptedPostgresBackupProvider(
        "postgresql+asyncpg://dbuser:dbpass@db.example:5432/app",
        tmp_path,
        key,
    )
    backup_id = "a" * 32
    backup_dir = tmp_path / backup_id
    backup_dir.mkdir(mode=0o700)
    plaintext = b"simulated pg_dump archive bytes"
    nonce = os.urandom(12)
    aad = _MAGIC + nonce
    encryptor = Cipher(algorithms.AES(provider._key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(aad)
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    encrypted_path = backup_dir / "database.dump.aesgcm"
    _write_private_file(encrypted_path, aad + ciphertext + encryptor.tag)
    artifact = BackupArtifact(
        backup_id=backup_id,
        scope=BackupScope.SERVICE,
        tenant_id=None,
        resource="postgres",
        created_at=datetime.now(UTC),
        storage_uri=encrypted_path.as_uri(),
        checksum_sha256=hashlib.sha256(plaintext).hexdigest(),
        encrypted=True,
        size_bytes=len(plaintext),
    )
    metadata = _artifact_payload(artifact)
    metadata["metadata_hmac"] = provider._metadata_signature(metadata)
    (backup_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    plaintext_path, digest, size = provider._decrypt_to_temp(artifact)
    try:
        assert plaintext_path.read_bytes() == plaintext
        assert digest == artifact.checksum_sha256
        assert size == artifact.size_bytes
    finally:
        plaintext_path.unlink(missing_ok=True)

    encrypted_path.write_bytes(encrypted_path.read_bytes()[:-1] + b"x")
    with pytest.raises(InvalidTag):
        provider._decrypt_to_temp(artifact)


@pytest.mark.asyncio
async def test_restore_requires_an_explicit_allow_list_without_running_pg_restore(tmp_path: Path):
    provider = EncryptedPostgresBackupProvider(
        "postgresql+asyncpg://dbuser:dbpass@db.example:5432/app",
        tmp_path,
        Fernet.generate_key().decode("ascii"),
    )

    with pytest.raises(PermissionError):
        await provider.restore_backup("a" * 32, "postgresql://user:pass@restore.example/db")


def test_backup_metadata_mac_changes_when_metadata_is_tampered(tmp_path: Path):
    key = Fernet.generate_key().decode("ascii")
    provider = EncryptedPostgresBackupProvider(
        "postgresql://dbuser:dbpass@db.example:5432/app", tmp_path, key
    )
    payload = {"backup_id": "a" * 32, "resource": "postgres"}
    signature = provider._metadata_signature(payload)

    assert signature == provider._metadata_signature(payload)
    assert signature != provider._metadata_signature({**payload, "resource": "other"})


def test_gcs_backup_metadata_accepts_legacy_s3_uri(tmp_path: Path):
    from io import BytesIO

    class MemoryGCS:
        uri_scheme = "gs"

        def __init__(self):
            self.objects = {}

        def get_object(self, *, Bucket, Key):
            return {"Body": BytesIO(self.objects[(Bucket, Key)])}

    client = MemoryGCS()
    bucket = "chiefvoice-service"
    backup_id = "b" * 32
    encryption_key = Fernet.generate_key().decode("ascii")

    provider = EncryptedPostgresBackupProvider(
        "postgresql+asyncpg://dbuser:dbpass@db.example:5432/app",
        tmp_path,
        encryption_key,
        s3_client=client,
        s3_bucket=bucket,
        s3_prefix="backups",
    )

    backup_key = provider._backup_key(backup_id, "database.dump.aesgcm")
    legacy_uri = f"s3://{bucket}/{backup_key}"
    artifact = BackupArtifact(
        backup_id=backup_id,
        scope=BackupScope.SERVICE,
        tenant_id=None,
        resource="postgres",
        created_at=datetime.now(UTC),
        storage_uri=legacy_uri,
        checksum_sha256="a" * 64,
        encrypted=True,
        size_bytes=100,
    )

    payload = _artifact_payload(artifact)
    payload["metadata_hmac"] = provider._metadata_signature(payload)
    metadata_key = provider._backup_key(backup_id, "metadata.json")
    client.objects[(bucket, metadata_key)] = (
        json.dumps(payload).encode("utf-8")
    )

    restored_metadata = provider._read_metadata(backup_id)

    assert restored_metadata is not None
    assert restored_metadata["storage_uri"] == legacy_uri
