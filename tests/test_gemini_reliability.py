from __future__ import annotations

from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    OutputAudioRawFrame,
    TTSAudioRawFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from chiefvoice.agents.call.audio_diagnostics import CallAudioDiagnostics
from chiefvoice.agents.call.conversation_reliability import GeminiConversationWatchdog
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
        self._bot_is_responding = False
        self.interruptions = 0
        self.sent = []
        self.reconnects = 0
        self.pushed_frames = []
        self._chiefvoice_hangup_callback = None

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

    async def broadcast_interruption(self):
        self.interruptions += 1

    async def _handle_interruption(self):
        self._bot_is_responding = False

    async def push_frame(self, frame):
        self.pushed_frames.append(frame)

    def create_task(self, coroutine, name=None):
        import asyncio

        return asyncio.create_task(coroutine, name=name)


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


@pytest.mark.asyncio
async def test_server_interruption_requires_local_caller_speech_confirmation(monkeypatch):
    import chiefvoice.agents.call.gemini_reliability as reliability

    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="vad-session"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="vad-session",
        diagnostics=diagnostics,
    )
    service = service_class()

    await service.broadcast_interruption()
    assert service.interruptions == 0
    assert diagnostics._vad_events["gemini_interruption_suppressed"] == 1

    service._user_is_speaking = True
    service._bot_is_responding = True
    original_sleep = reliability.asyncio.sleep

    async def short_wait(_seconds):
        await original_sleep(0)

    monkeypatch.setattr(reliability.asyncio, "sleep", short_wait)
    await service.broadcast_interruption()
    assert service._chiefvoice_pending_interruption_task is not None
    await service._chiefvoice_pending_interruption_task
    assert service.interruptions == 1
    assert diagnostics._vad_events["gemini_interruption_accepted"] == 1


@pytest.mark.asyncio
async def test_interruption_matching_recent_model_transcript_is_suppressed_as_echo(monkeypatch):
    import chiefvoice.agents.call.gemini_reliability as reliability

    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="echo-session"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="echo-session",
        diagnostics=diagnostics,
    )
    service = service_class()
    service._bot_is_responding = True
    service._user_is_speaking = True
    service._chiefvoice_recent_input_transcript = "please tell me your name and address"
    service._chiefvoice_recent_output_transcript = "please tell me your name and address"
    service._chiefvoice_recent_input_transcript_at = reliability.time.monotonic()
    service._chiefvoice_recent_output_transcript_at = reliability.time.monotonic()

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(reliability.asyncio, "sleep", no_wait)
    await service._confirm_interruption_after_echo_check()

    assert service.interruptions == 0
    assert diagnostics._vad_events["gemini_echo_interruption_suppressed"] == 1
    assert diagnostics._vad_events["gemini_interruption_accepted"] == 0


@pytest.mark.asyncio
async def test_watchdog_counts_local_vad_and_recovers_then_falls_back(monkeypatch):
    import chiefvoice.agents.call.conversation_reliability as reliability

    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="watchdog-session"))
    calls = []

    class WatchdogService:
        _session = object()
        _ready_for_realtime_input = True
        _bot_is_responding = False

        async def recover_unanswered_turn(self):
            calls.append("recover")
            return True

        async def play_fallback_and_hangup(self):
            calls.append("fallback")
            return True

    async def no_wait(_seconds):
        return None

    async def discard_frame(_frame, _direction=FrameDirection.DOWNSTREAM):
        return None

    monkeypatch.setattr(reliability.asyncio, "sleep", no_wait)
    watchdog = GeminiConversationWatchdog(
        SimpleNamespace(session_id="watchdog-session", provider="vobiz"),
        WatchdogService(),
        diagnostics,
        response_timeout_seconds=0,
        recovery_grace_seconds=0,
    )
    watchdog.push_frame = discard_frame
    await watchdog.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await watchdog._watch_turn(0, "test")

    assert diagnostics._vad_events["caller_speech_started"] == 1
    assert diagnostics._vad_events["response_watchdog_fired"] == 1
    assert calls == ["recover", "fallback"]


@pytest.mark.asyncio
async def test_watchdog_fallback_plays_bundled_pcm_then_requests_hangup():
    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="fallback-session"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="fallback-session",
        diagnostics=diagnostics,
    )
    service = service_class()
    hangups = []
    service._bot_is_responding = True

    async def hang_up():
        hangups.append(True)

    service._chiefvoice_hangup_callback = hang_up

    assert await service.play_fallback_and_hangup() is True
    assert service._bot_is_responding is False
    assert diagnostics._vad_events["response_watchdog_fallback"] == 1
    assert len(hangups) == 1
    audio_frames = [frame for frame in service.pushed_frames if isinstance(frame, TTSAudioRawFrame)]
    assert len(audio_frames) == 1
    assert isinstance(audio_frames[0], OutputAudioRawFrame)
    assert audio_frames[0].sample_rate == 24000
    assert audio_frames[0].audio
