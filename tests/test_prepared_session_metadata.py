import asyncio
import time

from chiefvoice.agents.call.prepared_session import PreparedRealtimeSession
from chiefvoice.sessions import AcceptedSession, Direction


def test_prepared_and_live_sessions_share_runtime_metadata():
    hangup_callback = object()
    preflight_session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        metadata={"voice_language": "ta-IN", "caller_turn_count": 0},
    )
    live_session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        metadata={"hang_up_call": hangup_callback, "caller_turn_count": 3},
    )
    prepared = PreparedRealtimeSession(
        processors=(),
        task_manager=object(),
        runtime_metadata=preflight_session.metadata,
        created_at=time.monotonic(),
        cleanup_lock=asyncio.Lock(),
    )

    prepared.bind_runtime_metadata(live_session)

    assert live_session.metadata is preflight_session.metadata
    assert preflight_session.metadata["caller_turn_count"] == 3
    assert preflight_session.metadata["hang_up_call"] is hangup_callback
    assert preflight_session.metadata["voice_language"] == "ta-IN"
