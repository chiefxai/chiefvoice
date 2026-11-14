from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from chiefvoice.calls.routes import get_call_recording
from chiefvoice.identity.auth import Principal


class _CallStore:
    async def get_recording_uri(self, tenant_id, session_id):
        if tenant_id == "tenant-a" and session_id == "session-a":
            return "opaque://private-storage-location"
        return None


class _RecordingStore:
    async def read_wav(self, tenant_id, session_id, recording_uri):
        assert tenant_id == "tenant-a"
        assert session_id == "session-a"
        assert recording_uri == "opaque://private-storage-location"
        return b"RIFF-audio"


def _request():
    state = SimpleNamespace(
        call_session_store=_CallStore(), recording_store=_RecordingStore()
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.mark.asyncio
async def test_recording_route_streams_audio_without_storage_reference():
    result = await get_call_recording(
        "session-a",
        _request(),
        Principal("user-a", "tenant-a", frozenset({"calls:read"})),
    )

    assert result.body == b"RIFF-audio"
    assert result.media_type == "audio/wav"
    assert result.headers["cache-control"] == "private, no-store"
    assert "private-storage-location" not in result.headers["content-disposition"]


@pytest.mark.asyncio
async def test_recording_route_hides_calls_owned_by_other_tenants():
    with pytest.raises(HTTPException) as exc_info:
        await get_call_recording(
            "session-a",
            _request(),
            Principal("user-b", "tenant-b", frozenset({"calls:read"})),
        )

    assert exc_info.value.status_code == 404
