from __future__ import annotations

from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    OutputAudioRawFrame,
    TTSAudioRawFrame,
)

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
        self._session_resumption_handle = None
        self._context = None
        self._bot_is_responding = False
        self.interruptions = 0
        self.sent = []
        self.reconnects = 0
        self.pushed_frames = []
        self._chiefvoice_hangup_callback = None
        self.reconnect_started = None
        self.reconnect_gate = None

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
        if self.reconnect_started is not None:
            self.reconnect_started.set()
        if self.reconnect_gate is not None:
            await self.reconnect_gate.wait()
        else:
            await asyncio.sleep(0.01)

    async def _disconnect(self):
        self._disconnecting = True
        self._session = None
        self._ready_for_realtime_input = False
        self._disconnecting = False

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
        self.texts = []
        self.text_started = None
        self.text_gate = None
        self.closed = False

    async def close(self):
        self.closed = True

    async def send_realtime_input(self, *, audio=None, text=None):
        if audio is not None:
            self.audio.append(audio.data)
        if text is not None:
            if self.text_started is not None:
                self.text_started.set()
            if self.text_gate is not None:
                await self.text_gate.wait()
            self.texts.append(text)


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
        "preroll_buffered": 0,
        "sent": 2,
        "dropped": 0,
        "preroll_buffered": 0,
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
async def test_recovery_times_out_when_gemini_never_becomes_ready():
    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="never-ready"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="never-ready",
        diagnostics=diagnostics,
    )
    service = service_class()
    service._chiefvoice_recovery_ready_timeout_seconds = 0.01

    assert await service.recover_unanswered_turn() is False
    assert service._chiefvoice_ready_event.is_set() is False
    assert service._chiefvoice_recovery_reconnect_task is None


@pytest.mark.asyncio
async def test_recovery_resumes_when_session_ready_event_arrives():
    import asyncio

    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="readiness-restored"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="readiness-restored",
        diagnostics=diagnostics,
    )
    service = service_class()
    service._chiefvoice_recovery_ready_timeout_seconds = 1
    service._context = object()
    service.reconnect_started = asyncio.Event()
    recovery = asyncio.create_task(service.recover_unanswered_turn())
    await asyncio.wait_for(service.reconnect_started.wait(), timeout=1)

    await service._handle_session_ready(FakeSession())

    assert await recovery is True
    assert service._chiefvoice_ready_event.is_set()
    assert service._chiefvoice_recovery_reconnect_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_state", ["fallback", "terminal"])
async def test_late_session_ready_does_not_restore_gemini_after_shutdown(shutdown_state):
    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="fallback-race"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="fallback-race",
        diagnostics=diagnostics,
    )
    service = service_class()
    if shutdown_state == "fallback":
        service._chiefvoice_fallback_active = True
    else:
        service._chiefvoice_terminal = True
    session = FakeSession()

    await service._handle_session_ready(session)

    assert session.closed
    assert service._session is None
    assert service._ready_for_realtime_input is False
    assert service._chiefvoice_ready_event.is_set() is False


@pytest.mark.asyncio
async def test_unanswered_turn_recovery_does_not_start_after_terminal_shutdown():
    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="terminal-recovery"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="terminal-recovery",
        diagnostics=diagnostics,
    )
    service = service_class()
    service._chiefvoice_terminal = True

    assert await service.recover_unanswered_turn() is False
    assert service.reconnects == 0
    assert service._chiefvoice_recovery_reconnect_task is None


@pytest.mark.asyncio
async def test_caller_inactivity_prompt_does_not_start_after_terminal_shutdown():
    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="terminal-prompt"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="terminal-prompt",
        diagnostics=diagnostics,
    )
    service = service_class()
    session = FakeSession()
    service._session = session
    service._ready_for_realtime_input = True
    service._chiefvoice_terminal = True

    assert await service.prompt_caller_inactivity("Are you still there?") is False
    assert session.texts == []


@pytest.mark.asyncio
async def test_recovery_timeout_also_bounds_resume_prompt_send():
    import asyncio

    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="stuck-resume-prompt"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="stuck-resume-prompt",
        diagnostics=diagnostics,
    )
    service = service_class()
    service._chiefvoice_recovery_ready_timeout_seconds = 0.05
    service.reconnect_started = asyncio.Event()
    session = FakeSession()
    session.text_started = asyncio.Event()
    session.text_gate = asyncio.Event()

    recovery = asyncio.create_task(service.recover_unanswered_turn())
    await asyncio.wait_for(service.reconnect_started.wait(), timeout=1)
    await service._handle_session_ready(session)

    assert await recovery is False
    assert session.text_started.is_set()


@pytest.mark.asyncio
async def test_reconnect_that_blocks_before_readiness_wait_is_bounded_and_cancelled():
    import asyncio

    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="blocked-reconnect"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="blocked-reconnect",
        diagnostics=diagnostics,
    )
    service = service_class()
    service._chiefvoice_recovery_ready_timeout_seconds = 0.05
    service.reconnect_started = asyncio.Event()
    service.reconnect_gate = asyncio.Event()
    recovery = asyncio.create_task(service.recover_unanswered_turn())
    await asyncio.wait_for(service.reconnect_started.wait(), timeout=1)
    reconnect = service._chiefvoice_recovery_reconnect_task
    assert reconnect is not None and not reconnect.done()

    assert await recovery is False

    assert service.reconnect_started.is_set()
    assert reconnect.cancelled()
    assert service._chiefvoice_recovery_reconnect_task is None


@pytest.mark.asyncio
async def test_ordinary_reconnect_is_not_limited_by_recovery_timeout():
    import asyncio

    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="ordinary-reconnect"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="ordinary-reconnect",
        diagnostics=diagnostics,
    )
    service = service_class()
    service._chiefvoice_recovery_ready_timeout_seconds = 0
    service.reconnect_started = asyncio.Event()
    service.reconnect_gate = asyncio.Event()
    reconnect = asyncio.create_task(service._reconnect())
    await asyncio.wait_for(service.reconnect_started.wait(), timeout=1)

    # Zero would immediately cancel this if an ordinary reconnect inherited
    # the watchdog recovery timeout.
    assert not reconnect.done()
    service.reconnect_gate.set()
    await reconnect

    assert service.reconnects == 1


@pytest.mark.asyncio
async def test_cancelled_recovery_cancels_its_shielded_reconnect_task():
    import asyncio

    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="cancelled-recovery"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="cancelled-recovery",
        diagnostics=diagnostics,
    )
    service = service_class()
    service.reconnect_started = asyncio.Event()
    service.reconnect_gate = asyncio.Event()
    recovery = asyncio.create_task(service.recover_unanswered_turn())
    await asyncio.wait_for(service.reconnect_started.wait(), timeout=1)
    reconnect = service._chiefvoice_recovery_reconnect_task
    assert reconnect is not None and not reconnect.done()

    recovery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovery

    assert reconnect.cancelled()
    assert service._chiefvoice_recovery_reconnect_task is None


@pytest.mark.asyncio
async def test_call_disconnect_cancels_pending_recovery_reconnect():
    import asyncio

    diagnostics = CallAudioDiagnostics(SimpleNamespace(session_id="terminated"))
    service_class = reliable_vertex_service_class(
        FakeGeminiService,
        session_id="terminated",
        diagnostics=diagnostics,
    )
    service = service_class()
    service.reconnect_started = asyncio.Event()
    service.reconnect_gate = asyncio.Event()
    recovery = asyncio.create_task(service.recover_unanswered_turn())
    await asyncio.wait_for(service.reconnect_started.wait(), timeout=1)
    reconnect = service._chiefvoice_recovery_reconnect_task
    assert reconnect is not None and not reconnect.done()

    await service._disconnect()

    assert reconnect.cancelled()
    assert service._chiefvoice_recovery_reconnect_task is None
    with pytest.raises(asyncio.CancelledError):
        await recovery


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
    assert await service.play_fallback_and_hangup() is False
    assert service._bot_is_responding is False
    assert service._session is None
    assert service._ready_for_realtime_input is False
    assert diagnostics._vad_events["response_watchdog_fallback"] == 1
    assert len(hangups) == 1
    audio_frames = [frame for frame in service.pushed_frames if isinstance(frame, TTSAudioRawFrame)]
    assert len(audio_frames) == 1
    assert isinstance(audio_frames[0], OutputAudioRawFrame)
    assert audio_frames[0].sample_rate == 24000
    assert audio_frames[0].audio
