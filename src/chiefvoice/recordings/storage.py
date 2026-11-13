import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor

from chiefvoice.core.config import settings
from chiefvoice.data_lifecycle.retention import RetentionClass
from chiefvoice.sessions import AcceptedSession

logger = logging.getLogger(__name__)


class RecordingStore(Protocol):
    async def save_wav(
        self,
        tenant_id: str,
        session_id: str,
        audio: bytes,
        sample_rate: int,
        channels: int,
    ) -> str | None: ...


class LocalRecordingStore:
    """Unencrypted development-only WAV storage with owner-only file permissions."""

    data_class = RetentionClass.RECORDING

    def __init__(self, directory: Path = settings.recordings_dir):
        self.directory = directory

    async def save_wav(
        self,
        tenant_id: str,
        session_id: str,
        audio: bytes,
        sample_rate: int,
        channels: int,
    ) -> str | None:
        if not audio or not tenant_id:
            return None
        # Keep tenants in separate directories without exposing tenant IDs in paths.
        tenant_digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
        tenant_directory = self.directory / tenant_digest
        path = tenant_directory / f"{safe_id}_{uuid4().hex[:8]}.wav"
        tenant_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(tenant_directory, 0o700)
        with io.BytesIO() as buffer:
            with wave.open(buffer, "wb") as wav:
                wav.setsampwidth(2)
                wav.setnchannels(channels)
                wav.setframerate(sample_rate)
                wav.writeframes(audio)
            wav_bytes = buffer.getvalue()
        await asyncio.to_thread(_write_private_file, path, wav_bytes)
        return str(path)

    async def purge_expired(self, tenant_id: str, cutoff: datetime, batch_size: int) -> int:
        """Delete a bounded number of expired development WAV files for one tenant."""
        if not tenant_id or cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("Tenant and timezone-aware cutoff are required")
        if batch_size < 1:
            raise ValueError("Purge batch size must be positive")
        tenant_directory = self.directory / _tenant_digest(tenant_id)
        return await asyncio.to_thread(
            _purge_plain_recordings, tenant_directory, cutoff, batch_size
        )


class EncryptedLocalRecordingStore:
    """Tenant-isolated local WAV storage encrypted with Fernet before writing to disk."""

    data_class = RetentionClass.RECORDING

    def __init__(self, directory: Path, encryption_key: str):
        self.directory = directory
        self._fernet = Fernet(encryption_key.encode("ascii"))

    async def save_wav(
        self,
        tenant_id: str,
        session_id: str,
        audio: bytes,
        sample_rate: int,
        channels: int,
    ) -> str | None:
        if not audio or not tenant_id:
            return None
        if sample_rate < 1 or channels < 1:
            raise ValueError("Recording sample rate and channel count must be positive")
        wav_bytes = _make_wav(audio, sample_rate, channels)
        tenant_digest = _tenant_digest(tenant_id)
        tenant_directory = self.directory / tenant_digest
        record_id = f"{_safe_record_id(session_id)}_{uuid4().hex}"
        temporary_directory = tenant_directory / f".{record_id}.tmp"
        record_directory = tenant_directory / record_id
        created_at = datetime.now(UTC)
        metadata = {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "created_at": created_at.isoformat(),
            "legal_hold": False,
        }
        encrypted_audio = self._fernet.encrypt(wav_bytes)
        encrypted_metadata = self._fernet.encrypt(
            json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        )
        await asyncio.to_thread(
            _write_encrypted_recording,
            tenant_directory,
            temporary_directory,
            record_directory,
            encrypted_audio,
            encrypted_metadata,
        )
        return str(record_directory / "audio.fernet")

    async def purge_expired(self, tenant_id: str, cutoff: datetime, batch_size: int) -> int:
        """Delete a bounded number of expired tenant recordings, preserving holds."""
        if not tenant_id or cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("Tenant and timezone-aware cutoff are required")
        if batch_size < 1:
            raise ValueError("Purge batch size must be positive")
        tenant_directory = self.directory / _tenant_digest(tenant_id)
        return await asyncio.to_thread(
            self._purge_expired_sync, tenant_directory, tenant_id, cutoff, batch_size
        )

    async def set_legal_hold(self, tenant_id: str, recording_id: str, held: bool) -> bool:
        """Set a durable hold after confirming the encrypted metadata belongs to tenant."""
        tenant_directory = self.directory / _tenant_digest(tenant_id)
        return await asyncio.to_thread(
            self._set_legal_hold_sync, tenant_directory, tenant_id, recording_id, held
        )

    async def delete_recording(self, tenant_id: str, recording_id: str) -> bool:
        """Delete one tenant-owned recording unless its metadata places it on legal hold."""
        tenant_directory = self.directory / _tenant_digest(tenant_id)
        return await asyncio.to_thread(
            self._delete_recording_sync, tenant_directory, tenant_id, recording_id
        )

    def _purge_expired_sync(
        self, tenant_directory: Path, tenant_id: str, cutoff: datetime, batch_size: int
    ) -> int:
        if not tenant_directory.is_dir() or tenant_directory.is_symlink():
            return 0
        deleted = 0
        candidates = 0
        try:
            entries = sorted(tenant_directory.iterdir(), key=lambda path: path.stat().st_mtime)
        except OSError:
            logger.exception("recording_retention_scan_failed")
            return 0
        for record_directory in entries:
            if record_directory.is_symlink() or not record_directory.is_dir():
                continue
            try:
                if datetime.fromtimestamp(record_directory.stat().st_mtime, UTC) > cutoff:
                    continue
            except OSError:
                continue
            candidates += 1
            if candidates > batch_size:
                break
            metadata = self._read_metadata(record_directory)
            if metadata is None or metadata.get("tenant_id") != tenant_id:
                continue
            if metadata.get("legal_hold") is True:
                continue
            try:
                created_at = datetime.fromisoformat(metadata["created_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if created_at.tzinfo is None or created_at > cutoff:
                continue
            try:
                shutil.rmtree(record_directory)
                deleted += 1
            except OSError:
                logger.exception("recording_retention_delete_failed")
        return deleted

    def _set_legal_hold_sync(
        self, tenant_directory: Path, tenant_id: str, recording_id: str, held: bool
    ) -> bool:
        record_directory = _record_directory(tenant_directory, recording_id)
        if record_directory is None:
            return False
        metadata = self._read_metadata(record_directory)
        if metadata is None or metadata.get("tenant_id") != tenant_id:
            return False
        metadata["legal_hold"] = held is True
        encrypted = self._fernet.encrypt(
            json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        )
        try:
            _replace_private_file(record_directory / "metadata.fernet", encrypted)
            os.utime(record_directory, None)
            return True
        except OSError:
            logger.exception("recording_legal_hold_update_failed")
            return False

    def _delete_recording_sync(
        self, tenant_directory: Path, tenant_id: str, recording_id: str
    ) -> bool:
        record_directory = _record_directory(tenant_directory, recording_id)
        if record_directory is None:
            return False
        metadata = self._read_metadata(record_directory)
        if (
            metadata is None
            or metadata.get("tenant_id") != tenant_id
            or metadata.get("legal_hold") is True
        ):
            return False
        try:
            shutil.rmtree(record_directory)
            return True
        except OSError:
            logger.exception("recording_delete_failed")
            return False

    def _read_metadata(self, record_directory: Path) -> dict | None:
        if record_directory.is_symlink():
            return None
        metadata_path = record_directory / "metadata.fernet"
        if metadata_path.is_symlink() or not metadata_path.is_file():
            return None
        try:
            decrypted = self._fernet.decrypt(metadata_path.read_bytes())
            metadata = json.loads(decrypted.decode("utf-8"))
        except (OSError, InvalidToken, UnicodeDecodeError, json.JSONDecodeError):
            logger.error("recording_metadata_unreadable")
            return None
        return metadata if isinstance(metadata, dict) else None


def create_recording_store() -> RecordingStore | None:
    """Build the configured store, allowing plaintext files only for local development."""
    if not settings.recordings_enabled:
        return None
    if settings.recordings_encryption_key:
        return EncryptedLocalRecordingStore(
            settings.recordings_dir, settings.recordings_encryption_key
        )
    if settings.app_env.lower() == "local" and settings.recordings_allow_unencrypted_local:
        logger.warning("recordings_using_unencrypted_development_storage")
        return LocalRecordingStore(settings.recordings_dir)
    raise RuntimeError(
        "RECORDINGS_ENCRYPTION_KEY is required when recordings are enabled outside local development"
    )


def attach_recording(
    processor: AudioBufferProcessor,
    session: AcceptedSession,
    store: RecordingStore,
) -> None:
    """Persist the merged user/assistant WAV when Pipecat flushes its audio buffer."""
    if not settings.recordings_enabled or not session.recording_allowed:
        return

    @processor.event_handler("on_audio_data")
    async def on_audio_data(_processor, audio: bytes, sample_rate: int, channels: int):
        if session.tenant_id is None:
            logger.error(
                "recording_skipped_missing_tenant",
                extra={"session_id": session.session_id, "provider": session.provider},
            )
            return
        path = await store.save_wav(
            session.tenant_id, session.session_id, audio, sample_rate, channels
        )
        if path:
            logger.info(
                "recording_saved",
                extra={
                    "session_id": session.session_id,
                    "channel": session.channel,
                    "provider": session.provider,
                },
            )


def create_recording_processor(session: AcceptedSession) -> AudioBufferProcessor:
    """Create Pipecat's audio recorder with recording gated by session consent and config."""
    return AudioBufferProcessor(
        auto_start_recording=settings.recordings_enabled and session.recording_allowed
    )


def _write_private_file(path: Path, contents: bytes) -> None:
    """Create a recording file with owner-only permissions on POSIX systems."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as recording_file:
            recording_file.write(contents)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _make_wav(audio: bytes, sample_rate: int, channels: int) -> bytes:
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as wav:
            wav.setsampwidth(2)
            wav.setnchannels(channels)
            wav.setframerate(sample_rate)
            wav.writeframes(audio)
        return buffer.getvalue()


def _write_encrypted_recording(
    tenant_directory: Path,
    temporary_directory: Path,
    record_directory: Path,
    encrypted_audio: bytes,
    encrypted_metadata: bytes,
) -> None:
    tenant_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(tenant_directory, 0o700)
    temporary_directory.mkdir(mode=0o700)
    try:
        _write_private_file(temporary_directory / "audio.fernet", encrypted_audio)
        _write_private_file(temporary_directory / "metadata.fernet", encrypted_metadata)
        os.rename(temporary_directory, record_directory)
        os.chmod(record_directory, 0o700)
    except BaseException:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise


def _replace_private_file(path: Path, contents: bytes) -> None:
    temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        _write_private_file(temporary_path, contents)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _tenant_digest(tenant_id: str) -> str:
    return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()


def _safe_record_id(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:128]


def _record_directory(tenant_directory: Path, recording_id: str) -> Path | None:
    if not recording_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", recording_id):
        return None
    directory = tenant_directory / recording_id
    if directory.is_symlink() or not directory.is_dir():
        return None
    return directory


def _purge_plain_recordings(tenant_directory: Path, cutoff: datetime, batch_size: int) -> int:
    if not tenant_directory.is_dir() or tenant_directory.is_symlink():
        return 0
    deleted = 0
    try:
        candidates = sorted(tenant_directory.glob("*.wav"), key=lambda path: path.stat().st_mtime)
        cutoff_timestamp = cutoff.timestamp()
        for path in candidates:
            if deleted >= batch_size:
                break
            if path.is_symlink() or not path.is_file() or path.stat().st_mtime > cutoff_timestamp:
                continue
            path.unlink()
            deleted += 1
    except OSError:
        logger.exception("recording_retention_scan_failed")
    return deleted
