"""Encrypted, service-scoped PostgreSQL backups using pg_dump and pg_restore."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from chiefvoice.data_lifecycle.backup import (
    BackupArtifact,
    BackupPolicy,
    BackupScope,
)

logger = logging.getLogger(__name__)

_MAGIC = b"CHIEFVOICE-PG-BACKUP-V1\n"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_CHUNK_BYTES = 1024 * 1024
_BACKUP_ID = re.compile(r"^[0-9a-f]{32}$")


class EncryptedPostgresBackupProvider:
    """Write pg_dump custom archives encrypted with authenticated AES-GCM.

    ``database_url`` is parsed into libpq environment variables; credentials are never
    placed in a process argument or a log. Restore is disabled unless a target DSN is
    explicitly allow-listed, and restoring into the configured source database is rejected.
    The provider intentionally supports service backups only: tenant data shares a database.
    """

    def __init__(
        self,
        database_url: str,
        directory: Path,
        encryption_key: str,
        *,
        allowed_restore_targets: tuple[str, ...] = (),
        resource_name: str = "postgres",
        pg_dump_executable: str = "pg_dump",
        pg_restore_executable: str = "pg_restore",
    ):
        self._source_env, self._source_identity = _libpq_environment(database_url)
        self.directory = Path(directory).resolve()
        self.resource_name = resource_name
        self.pg_dump_executable = pg_dump_executable
        self.pg_restore_executable = pg_restore_executable
        try:
            key = base64.urlsafe_b64decode(encryption_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("Backup key must be a URL-safe base64 Fernet key") from exc
        if len(key) != 32:
            raise ValueError("Backup key must decode to 32 bytes")
        self._key = key
        self._metadata_key = hmac.digest(key, b"chiefvoice-backup-metadata-v1", "sha256")
        self._allowed_targets: dict[str, tuple[dict[str, str], tuple[str, str, str]]] = {}
        for target in allowed_restore_targets:
            target_env, identity = _libpq_environment(target)
            if identity == self._source_identity:
                raise ValueError("Restore target cannot be the configured source database")
            self._allowed_targets[target] = (target_env, identity)

    async def create_backup(self, policy: BackupPolicy) -> BackupArtifact:
        self._validate_policy(policy)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        backup_id = uuid4().hex
        staging_directory = self.directory / f".{backup_id}.tmp"
        final_directory = self.directory / backup_id
        staging_directory.mkdir(mode=0o700)
        encrypted_path = staging_directory / "database.dump.aesgcm"
        try:
            created_at, checksum, size_bytes = await self._dump_encrypted(encrypted_path)
            artifact = BackupArtifact(
                backup_id=backup_id,
                scope=policy.scope,
                tenant_id=None,
                resource=policy.resource,
                created_at=created_at,
                storage_uri=(final_directory / encrypted_path.name).as_uri(),
                checksum_sha256=checksum,
                encrypted=True,
                size_bytes=size_bytes,
            )
            metadata = _artifact_payload(artifact)
            metadata["metadata_hmac"] = self._metadata_signature(metadata)
            _write_private_file(
                staging_directory / "metadata.json",
                json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
            os.rename(staging_directory, final_directory)
            os.chmod(final_directory, 0o700)
            return artifact
        except BaseException:
            shutil.rmtree(staging_directory, ignore_errors=True)
            raise

    async def get_backup(self, backup_id: str) -> BackupArtifact | None:
        metadata = await asyncio.to_thread(self._read_metadata, backup_id)
        return _artifact_from_payload(metadata) if metadata else None

    async def list_backups(self, policy: BackupPolicy) -> list[BackupArtifact]:
        self._validate_policy(policy)
        if not self.directory.is_dir() or self.directory.is_symlink():
            return []
        artifacts: list[BackupArtifact] = []
        for entry in await asyncio.to_thread(lambda: list(self.directory.iterdir())):
            if not _BACKUP_ID.fullmatch(entry.name) or entry.is_symlink() or not entry.is_dir():
                continue
            metadata = await asyncio.to_thread(self._read_metadata, entry.name)
            artifact = _artifact_from_payload(metadata) if metadata else None
            if artifact and artifact.resource == policy.resource:
                artifacts.append(artifact)
        return sorted(artifacts, key=lambda item: item.created_at)

    async def verify_backup(self, backup_id: str) -> bool:
        artifact = await self.get_backup(backup_id)
        if artifact is None or not artifact.encrypted:
            return False
        temporary_path: Path | None = None
        try:
            temporary_path, digest, size = await asyncio.to_thread(
                self._decrypt_to_temp, artifact
            )
            if digest != artifact.checksum_sha256 or size != artifact.size_bytes:
                return False
            process = await asyncio.create_subprocess_exec(
                self.pg_restore_executable,
                "--list",
                str(temporary_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=_safe_process_environment(),
            )
            stderr_task = asyncio.create_task(_read_limited(process.stderr, 16_384))
            await process.wait()
            await stderr_task
            return process.returncode == 0
        except Exception:
            logger.error("backup_verification_failed", extra={"backup_id": backup_id})
            return False
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    async def restore_backup(self, backup_id: str, target: str) -> None:
        target_config = self._allowed_targets.get(target)
        if target_config is None:
            raise PermissionError("Restore target is not explicitly allow-listed")
        target_env, target_identity = target_config
        if target_identity == self._source_identity:
            raise PermissionError("Restore target cannot be the configured source database")
        artifact = await self.get_backup(backup_id)
        if artifact is None or not artifact.encrypted or artifact.resource != self.resource_name:
            raise ValueError("Backup artifact is missing, invalid, or incompatible")
        temporary_path: Path | None = None
        try:
            temporary_path, digest, size = await asyncio.to_thread(
                self._decrypt_to_temp, artifact
            )
            if digest != artifact.checksum_sha256 or size != artifact.size_bytes:
                raise ValueError("Backup checksum verification failed")
            process = await asyncio.create_subprocess_exec(
                self.pg_restore_executable,
                "--exit-on-error",
                "--no-owner",
                "--clean",
                "--if-exists",
                "--single-transaction",
                f"--dbname={target_env['PGDATABASE']}",
                str(temporary_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env={**_safe_process_environment(), **target_env},
            )
            stderr_task = asyncio.create_task(_read_limited(process.stderr, 16_384))
            await process.wait()
            await stderr_task
            if process.returncode != 0:
                raise RuntimeError("pg_restore exited unsuccessfully")
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    async def delete_backup(self, backup_id: str) -> None:
        await asyncio.to_thread(self._delete_sync, backup_id)

    def _validate_policy(self, policy: BackupPolicy) -> None:
        if policy.scope is not BackupScope.SERVICE or policy.tenant_id is not None:
            raise ValueError("PostgreSQL backups must be service-scoped")
        if policy.resource != self.resource_name:
            raise ValueError("Backup policy resource does not match this provider")

    async def _dump_encrypted(self, output_path: Path) -> tuple[datetime, str, int]:
        nonce = os.urandom(_NONCE_BYTES)
        aad = _MAGIC + nonce
        encryptor = Cipher(algorithms.AES(self._key), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(aad)
        digest = hashlib.sha256()
        plain_size = 0
        created_at = datetime.now(UTC)
        environment = {**_safe_process_environment(), **self._source_env}
        process = await asyncio.create_subprocess_exec(
            self.pg_dump_executable,
            "--format=custom",
            "--no-password",
            f"--dbname={self._source_env['PGDATABASE']}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        stderr_task = asyncio.create_task(_read_limited(process.stderr, 16_384))
        try:
            with output_path.open("xb") as output:
                os.chmod(output_path, 0o600)
                output.write(aad)
                while chunk := await process.stdout.read(_CHUNK_BYTES):
                    plain_size += len(chunk)
                    digest.update(chunk)
                    output.write(encryptor.update(chunk))
                output.write(encryptor.finalize())
                output.write(encryptor.tag)
                output.flush()
                os.fsync(output.fileno())
            await process.wait()
            await stderr_task
            if process.returncode != 0 or plain_size == 0:
                raise RuntimeError("pg_dump did not produce a valid database archive")
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            await stderr_task
            raise
        return created_at, digest.hexdigest(), plain_size

    def _read_metadata(self, backup_id: str) -> dict | None:
        if not _BACKUP_ID.fullmatch(backup_id):
            return None
        directory = self.directory / backup_id
        if directory.is_symlink() or not directory.is_dir():
            return None
        metadata_path = directory / "metadata.json"
        if metadata_path.is_symlink() or not metadata_path.is_file():
            return None
        try:
            payload = json.loads(metadata_path.read_text("utf-8"))
            if not isinstance(payload, dict):
                return None
            supplied_mac = payload.pop("metadata_hmac")
            expected_mac = self._metadata_signature(payload)
            if not isinstance(supplied_mac, str) or not hmac.compare_digest(
                supplied_mac, expected_mac
            ):
                return None
            if payload.get("backup_id") != backup_id:
                return None
            expected_file = (directory / "database.dump.aesgcm").resolve()
            if Path(payload["storage_uri"]).resolve() != expected_file:
                return None
            return payload
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _metadata_signature(self, payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._metadata_key, canonical, hashlib.sha256).hexdigest()

    def _decrypt_to_temp(self, artifact: BackupArtifact) -> tuple[Path, str, int]:
        backup_path = self.directory / artifact.backup_id / "database.dump.aesgcm"
        if backup_path.is_symlink() or not backup_path.is_file():
            raise ValueError("Encrypted backup payload is missing")
        total_size = backup_path.stat().st_size
        header_size = len(_MAGIC) + _NONCE_BYTES
        if total_size < header_size + _TAG_BYTES:
            raise ValueError("Encrypted backup payload is truncated")
        with tempfile.NamedTemporaryFile(
            prefix="chiefvoice-restore-", suffix=".dump", dir=self.directory, delete=False
        ) as plaintext:
            temporary_path = Path(plaintext.name)
            os.chmod(temporary_path, 0o600)
            try:
                with backup_path.open("rb") as encrypted:
                    header = encrypted.read(header_size)
                    if not header.startswith(_MAGIC):
                        raise ValueError("Encrypted backup header is invalid")
                    nonce = header[len(_MAGIC) :]
                    encrypted.seek(-_TAG_BYTES, os.SEEK_END)
                    tag = encrypted.read(_TAG_BYTES)
                    ciphertext_end = total_size - _TAG_BYTES
                    encrypted.seek(header_size)
                    decryptor = Cipher(
                        algorithms.AES(self._key), modes.GCM(nonce, tag)
                    ).decryptor()
                    decryptor.authenticate_additional_data(header)
                    digest = hashlib.sha256()
                    plain_size = 0
                    remaining = ciphertext_end - header_size
                    while remaining:
                        chunk = encrypted.read(min(_CHUNK_BYTES, remaining))
                        if not chunk:
                            raise ValueError("Encrypted backup payload is truncated")
                        remaining -= len(chunk)
                        output = decryptor.update(chunk)
                        digest.update(output)
                        plain_size += len(output)
                        plaintext.write(output)
                    final = decryptor.finalize()
                    digest.update(final)
                    plain_size += len(final)
                    plaintext.write(final)
                    plaintext.flush()
                    os.fsync(plaintext.fileno())
                return temporary_path, digest.hexdigest(), plain_size
            except BaseException:
                temporary_path.unlink(missing_ok=True)
                raise

    def _delete_sync(self, backup_id: str) -> None:
        if not _BACKUP_ID.fullmatch(backup_id):
            return
        directory = self.directory / backup_id
        if directory.is_symlink() or not directory.is_dir():
            return
        # Do not follow symlinks when deleting a tampered artifact directory.
        for child in directory.iterdir():
            if child.is_symlink() or not child.is_file():
                raise ValueError("Backup artifact contains an unexpected filesystem entry")
        shutil.rmtree(directory)


def _artifact_payload(artifact: BackupArtifact) -> dict:
    return {
        "backup_id": artifact.backup_id,
        "scope": artifact.scope.value,
        "tenant_id": artifact.tenant_id,
        "resource": artifact.resource,
        "created_at": artifact.created_at.astimezone(UTC).isoformat(),
        "storage_uri": artifact.storage_uri,
        "checksum_sha256": artifact.checksum_sha256,
        "encrypted": artifact.encrypted,
        "size_bytes": artifact.size_bytes,
    }


def _artifact_from_payload(payload: dict | None) -> BackupArtifact | None:
    if not payload:
        return None
    try:
        return BackupArtifact(
            backup_id=payload["backup_id"],
            scope=BackupScope(payload["scope"]),
            tenant_id=payload["tenant_id"],
            resource=payload["resource"],
            created_at=datetime.fromisoformat(payload["created_at"]),
            storage_uri=payload["storage_uri"],
            checksum_sha256=payload["checksum_sha256"],
            encrypted=payload["encrypted"] is True,
            size_bytes=int(payload["size_bytes"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _write_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _safe_process_environment() -> dict[str, str]:
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "TMPDIR",
        "PGSERVICEFILE",
        "PGSSLCERT",
        "PGSSLKEY",
        "PGSSLROOTCERT",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _libpq_environment(database_url: str) -> tuple[dict[str, str], tuple[str, str, str]]:
    try:
        parsed = urlsplit(database_url)
        if parsed.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
            raise ValueError
        database = unquote(parsed.path.lstrip("/"))
        host = parsed.hostname or "localhost"
        port = str(parsed.port or 5432)
        username = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        if not database or not username:
            raise ValueError
        query = parse_qs(parsed.query)
        environment = {
            "PGHOST": host,
            "PGPORT": port,
            "PGUSER": username,
            "PGDATABASE": database,
        }
        if password:
            environment["PGPASSWORD"] = password
        sslmode = query.get("sslmode", [None])[0]
        if sslmode:
            if sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
                raise ValueError
            environment["PGSSLMODE"] = sslmode
        return environment, (host, port, database)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Database URL must be a valid PostgreSQL URL with a database and user") from exc


async def _read_limited(stream: asyncio.StreamReader | None, limit: int) -> bytes:
    if stream is None:
        return b""
    captured = bytearray()
    while chunk := await stream.read(4096):
        if len(captured) < limit:
            captured.extend(chunk[: limit - len(captured)])
    return bytes(captured)
