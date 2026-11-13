import asyncio
import io
import os
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from chiefvoice.data_lifecycle.retention import StaticRetentionPolicyDirectory
from chiefvoice.recordings.storage import EncryptedLocalRecordingStore


def test_recordings_are_encrypted_and_tenant_isolated(tmp_path: Path):
    key = Fernet.generate_key().decode("ascii")
    store = EncryptedLocalRecordingStore(tmp_path, key)

    recording_uri = asyncio.run(
        store.save_wav("tenant-alpha", "session-a", b"\x00\x00", 8000, 1)
    )

    assert recording_uri is not None
    audio_path = Path(recording_uri)
    encrypted_audio = audio_path.read_bytes()
    assert encrypted_audio != b"\x00\x00"
    assert b"tenant-alpha" not in encrypted_audio
    with wave.open(io.BytesIO(Fernet(key).decrypt(encrypted_audio)), "rb") as wav:
        assert wav.getframerate() == 8000
        assert wav.getnchannels() == 1
        assert wav.readframes(1) == b"\x00\x00"
    encrypted_metadata = (audio_path.parent / "metadata.fernet").read_bytes()
    assert b"tenant-alpha" not in encrypted_metadata
    assert audio_path.stat().st_mode & 0o777 == 0o600
    assert audio_path.parent.parent.name != "tenant-alpha"


def test_retention_respects_recording_legal_hold(tmp_path: Path):
    store = EncryptedLocalRecordingStore(tmp_path, Fernet.generate_key().decode("ascii"))
    recording_uri = asyncio.run(
        store.save_wav("tenant-alpha", "session-a", b"\x00\x00", 8000, 1)
    )
    assert recording_uri is not None
    recording_id = Path(recording_uri).parent.name
    old_cutoff = datetime.now(UTC) + timedelta(seconds=1)

    assert asyncio.run(store.set_legal_hold("tenant-alpha", recording_id, True))
    assert asyncio.run(store.purge_expired("tenant-alpha", old_cutoff, 10)) == 0
    assert Path(recording_uri).exists()

    assert asyncio.run(store.set_legal_hold("tenant-alpha", recording_id, False))
    # Legal-hold updates touch the object; move its directory timestamp back to exercise expiry.
    os.utime(Path(recording_uri).parent, (0, 0))
    assert asyncio.run(store.purge_expired("tenant-alpha", old_cutoff, 10)) == 1
    assert not Path(recording_uri).exists()


def test_recording_deletion_cannot_cross_tenant(tmp_path: Path):
    store = EncryptedLocalRecordingStore(tmp_path, Fernet.generate_key().decode("ascii"))
    recording_uri = asyncio.run(
        store.save_wav("tenant-alpha", "session-a", b"\x00\x00", 8000, 1)
    )
    assert recording_uri is not None
    recording_id = Path(recording_uri).parent.name

    assert not asyncio.run(store.delete_recording("tenant-beta", recording_id))
    assert Path(recording_uri).exists()
    assert asyncio.run(store.delete_recording("tenant-alpha", recording_id))


def test_static_retention_configuration_is_empty_by_default():
    directory = StaticRetentionPolicyDirectory.from_json("")

    assert asyncio.run(directory.list_policies()) == []
    with pytest.raises(ValueError):
        StaticRetentionPolicyDirectory.from_json(
            '[{"tenant_id":"tenant-alpha","data_class":"recording",'
            '"retain_for_seconds":0}]'
        )
