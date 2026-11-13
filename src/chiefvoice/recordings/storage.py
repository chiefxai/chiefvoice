import asyncio
import hashlib
import io
import logging
import os
import re
import wave
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor

from chiefvoice.core.config import settings
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
