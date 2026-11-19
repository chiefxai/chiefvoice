import asyncio
import json

from chiefvoice.agents.call.audio_diagnostics import ObservedVobizSerializer
from chiefvoice.channels.vobiz.playback import VobizPlaybackTracker


class FakeDiagnostics:
    session_id = "test"

    def __init__(self):
        self.events = []

    def record_conversation_event(self, event, *, count=1):
        self.events.extend([event] * count)


class FakeWebsocket:
    def __init__(self):
        self.messages = []

    async def send_text(self, message):
        self.messages.append(message)


class FakeSerializer:
    async def deserialize(self, data):
        return None


async def test_checkpoint_acknowledgement_correlates_to_played_stream():
    diagnostics = FakeDiagnostics()
    tracker = VobizPlaybackTracker("test", diagnostics)
    websocket = FakeWebsocket()
    name = await tracker.send_checkpoint(websocket, "stream-1")
    payload = json.loads(websocket.messages[0])
    assert payload == {"event": "checkpoint", "streamId": "stream-1", "name": name}

    serializer = ObservedVobizSerializer(FakeSerializer(), diagnostics, tracker)
    await serializer.deserialize(json.dumps({"event": "playedStream", "name": name}))

    assert await tracker.wait_for_latest_ack(0.01) == "played"
    assert "vobiz_playback_ack_received" in diagnostics.events


async def test_checkpoint_timeout_is_reported():
    diagnostics = FakeDiagnostics()
    tracker = VobizPlaybackTracker("test", diagnostics)
    await tracker.send_checkpoint(FakeWebsocket(), "stream-1")

    assert await tracker.wait_for_latest_ack(0) == "timeout"
    assert "vobiz_playback_ack_timeout" in diagnostics.events


async def test_caller_activity_cancels_checkpoint_wait():
    diagnostics = FakeDiagnostics()
    tracker = VobizPlaybackTracker("test", diagnostics)
    await tracker.send_checkpoint(FakeWebsocket(), "stream-1")
    caller_activity = asyncio.Event()
    waiter = asyncio.create_task(tracker.wait_for_latest_ack(1, caller_activity))
    await asyncio.sleep(0)
    caller_activity.set()

    assert await waiter == "caller_activity"
