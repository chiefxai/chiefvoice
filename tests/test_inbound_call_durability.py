from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.responses import Response
from starlette.requests import Request

from chiefvoice.calls.coordinator import CallCoordinator
from chiefvoice.sessions import Direction


class _Adapter:
    name = "vobiz"
    provider = "vobiz"

    def __init__(self):
        self.accepted = 0
        self.rejected = []

    async def parse_inbound(self, request):
        return SimpleNamespace(
            provider="vobiz",
            channel="phone",
            tenant_id="tenant-1",
            caller="+15550000001",
            callee="+15550000002",
            provider_call_id="provider-call-1",
            bot_config_id=None,
        )

    async def accept_inbound(self, call, session):
        self.accepted += 1
        assert session.direction is Direction.INBOUND
        return session, Response(content="<Response/>", media_type="application/xml")

    async def reject_inbound(self, call, reason):
        self.rejected.append(reason)
        return Response(content="<Response><Hangup/></Response>", media_type="application/xml")


class _Subscriptions:
    def __init__(self):
        self.authorized = 0
        self.answered = 0
        self.released = 0

    async def authorize_voice(self, tenant_id, session_id, channel, direction):
        self.authorized += 1
        return SimpleNamespace(recording_allowed=True, max_call_seconds=600)

    async def mark_answered(self, tenant_id, session_id, answered_at):
        self.answered += 1
        return answered_at

    async def release_voice(self, tenant_id, session_id):
        self.released += 1

    async def settle_voice(self, tenant_id, session_id, seconds, *, ended_at):
        return False


class _CallStore:
    def __init__(self):
        self.sessions = {}

    async def register_inbound(self, session):
        existing = self.sessions.get(session.session_id)
        if existing:
            saved, status = existing
            return saved, False, status
        self.sessions[session.session_id] = (session, "starting")
        return session, True, "starting"

    async def update_state(self, session, *, status, **kwargs):
        self.sessions[session.session_id] = (session, status)

    async def mark_post_call_enqueued(self, tenant_id, session_id):
        pass


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/webhooks/vobiz/inbound"})


async def test_inbound_session_is_registered_and_lifecycle_is_persisted():
    adapter = _Adapter()
    subscriptions = _Subscriptions()
    store = _CallStore()
    coordinator = CallCoordinator(
        {"vobiz": adapter},
        subscriptions,
        post_call_required=False,
        call_session_store=store,
    )

    response = await coordinator.handle_inbound("vobiz", _request())
    session_id = next(iter(store.sessions))
    assert response.status_code == 200
    assert store.sessions[session_id][1] == "answered"
    assert subscriptions.authorized == 1
    assert subscriptions.answered == 1

    session = store.sessions[session_id][0]
    session.started_at = datetime.now(UTC)
    await coordinator.end_session(session, "completed")
    assert store.sessions[session_id][1] == "completed"


async def test_duplicate_inbound_callback_reuses_durable_call_id():
    adapter = _Adapter()
    subscriptions = _Subscriptions()
    store = _CallStore()
    coordinator = CallCoordinator(
        {"vobiz": adapter},
        subscriptions,
        post_call_required=False,
        call_session_store=store,
    )

    await coordinator.handle_inbound("vobiz", _request())
    first_session_id = next(iter(store.sessions))
    await coordinator.handle_inbound("vobiz", _request())

    assert list(store.sessions) == [first_session_id]
    assert subscriptions.authorized == 1
    assert adapter.accepted == 2
    assert len(store.sessions) == 1


async def test_missing_provider_call_id_is_rejected_when_durable_store_enabled():
    adapter = _Adapter()
    adapter.parse_inbound = lambda request: _missing_call_id()
    store = _CallStore()
    coordinator = CallCoordinator(
        {"vobiz": adapter},
        _Subscriptions(),
        post_call_required=False,
        call_session_store=store,
    )

    response = await coordinator.handle_inbound("vobiz", _request())

    assert response.status_code == 200
    assert adapter.rejected == ["Provider call ID is required for durable inbound call handling"]
    assert not store.sessions


async def _missing_call_id():
    return SimpleNamespace(
        provider="vobiz",
        channel="phone",
        tenant_id="tenant-1",
        caller="+15550000001",
        callee="+15550000002",
        provider_call_id=None,
        bot_config_id=None,
    )
