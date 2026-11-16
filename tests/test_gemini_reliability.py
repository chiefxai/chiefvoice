from __future__ import annotations

from types import SimpleNamespace

import pytest
from pipecat.frames.frames import InputAudioRawFrame

from chiefvoice.agents.call.audio_diagnostics import CallAudioDiagnostics
from chiefvoice.agents.call.gemini_reliability import (
    BoundedGeminiAudioBuffer,
    reliable_vertex_service_class,
)


def audio(payload: bytes) -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=payload, sample_rate=16000, num_channels=1)


def test_audio_buffer_keeps_order_and_evicts_oldest_with_hard_bounds():
    buffer = BoundedGeminiAudioBuffer(max_audio_bytes=6, max_frames=2, max_age_seconds=2)

    assert buffer.append(audio(b"aa"), now=1) == (True, 0)
    assert buffer.append(audio(b"bb"), now=1.1) == (True, 0)
    assert buffer.append(audio(b"cc"), now=1.2) == (True, 1)

    first, stale = buffer.pop(now=1.3)
    second, stale2 = buffer.pop(now=1.3)
    assert (first.audio, second.audio) == (b"bb", b"cc")
    assert stale + stale2 == 0
    assert len(buffer) == 0
    assert buffer.audio_bytes == 0


def test_audio_buffer_discards_stale_frames_without_reordering():
    buffer = BoundedGeminiAudioBuffer(max_audio_bytes=100, max_frames=10, max_age_seconds=1)
    buffer.append(audio(b"old"), now=1)
    buffer.append(audio(b"fresh"), now=1.5)

    frame, stale = buffer.pop(now=2.1)
    assert stale == 1
    assert frame.audio == b"fresh"


class FakeGeminiService:
    def __init__(self, **_kwargs):
        self._audio_input_paused = False
        self._disconnecting = False
        self._session = None
        self._ready_for_realtime_input = False
        self._vad_disabled = False
        self._user_is_speaking = False
        self._user_audio_preroll_buffer = bytearray()
        self._user_audio_preroll_secs = 2
        self._user_audio_preroll_buffer_sample_rate = None
        self._settings = SimpleNamespace(model="test-model")
        self._consecutive_failures = 0
        self.sent = []
        self.reconnects = 0

    async def _handle_connection_error(self, _error):
        self._consecutive_failures += 1
        return self._consecutive_failures < 3

    async def _handle_session_ready(self, session):
        self._session = session
        self._ready_for_realtime_input = True

    async def _handle_send_error(self, error):
        raise error

    async def _reconnect(self):
        self.reconnects += 1
        import asyncio

        await asyncio.sleep(0.01)


class FakeSession:
    def __init__(self):
        self.audio = []

    async def send_realtime_input(self, *, audio):
        self.audio.append(audio.data)


@pytest.mark.asyncio
async def test_frames_received_during_reconnect_replay_once_in_order(monkeypatch):
    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="test-session"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="test-session",
        diagnostics=diagnostics,
    )
    service = service_class()
    service._chiefvoice_audio_buffer = BoundedGeminiAudioBuffer(
        max_audio_bytes=100, max_frames=10, max_age_seconds=2
    )
    monkeypatch.setattr("chiefvoice.agents.call.gemini_reliability.time.monotonic", lambda: 1.0)

    await service._send_user_audio(audio(b"one"))
    await service._send_user_audio(audio(b"two"))
    assert service._session is None

    session = FakeSession()
    await service._handle_session_ready(session)
    await service._chiefvoice_flush_audio()

    assert session.audio == [b"one", b"two"]
    assert diagnostics._gemini_audio == {
        "received": 2,
        "queued": 2,
        "sent": 2,
        "dropped": 0,
    }
    assert len(service._chiefvoice_audio_buffer) == 0


@pytest.mark.asyncio
async def test_reconnect_backoff_is_exponential_and_failure_count_is_bounded(monkeypatch):
    import chiefvoice.agents.call.gemini_reliability as reliability

    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="retry-session"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="retry-session",
        diagnostics=diagnostics,
    )
    service = service_class()
    delays = []

    async def record_delay(delay):
        delays.append(delay)

    monkeypatch.setattr(reliability.asyncio, "sleep", record_delay)
    monkeypatch.setattr(reliability.random, "uniform", lambda _low, high: high)

    assert await service._handle_connection_error(RuntimeError("1011")) is True
    assert await service._handle_connection_error(RuntimeError("1011")) is True
    assert await service._handle_connection_error(RuntimeError("1011")) is False
    assert delays == [0.5, 1.0]
    assert diagnostics._gemini_reconnects == 2


@pytest.mark.asyncio
async def test_simultaneous_reconnect_requests_create_only_one_session():
    import asyncio

    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="lock-session"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="lock-session",
        diagnostics=diagnostics,
    )
    service = service_class()

    await asyncio.gather(service._reconnect(), service._reconnect())
    assert service.reconnects == 1
