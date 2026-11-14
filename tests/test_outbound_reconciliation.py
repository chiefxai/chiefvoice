from datetime import timedelta

import pytest

from chiefvoice.billing.models import BillingMode, VoiceEntitlement
from chiefvoice.calls.coordinator import CallCoordinator, OutboundCallCommand
from chiefvoice.calls.recovery import reconcile_stale_outbound_once
from chiefvoice.channels.base import AmbiguousCallStart


class _AmbiguousAdapter:
    name = "vobiz"
    provider = "vobiz"

    async def initiate_outbound(self, call, session):
        raise AmbiguousCallStart("response timed out")


class _Subscriptions:
    def __init__(self):
        self.released: list[str] = []

    async def authorize_voice(self, tenant_id, session_id, channel, direction):
        return VoiceEntitlement(
            tenant_id=tenant_id,
            billing_mode=BillingMode.PREPAID,
            can_use_voice=True,
            allowed_channels=frozenset({channel}),
            max_concurrent_sessions=1,
            recording_allowed=False,
        )

    async def release_voice(self, tenant_id, session_id):
        self.released.append(session_id)


class _CallStore:
    def __init__(self):
        self.status = None

    @staticmethod
    def fingerprint(data):
        return "fingerprint"

    async def register_outbound(self, session, idempotency_key, fingerprint):
        return session, True, "starting"

    async def update_state(self, session, *, status, **kwargs):
        self.status = status


@pytest.mark.asyncio
async def test_ambiguous_provider_start_is_not_released_or_redialed():
    subscriptions = _Subscriptions()
    store = _CallStore()
    coordinator = CallCoordinator(
        {"vobiz": _AmbiguousAdapter()},
        subscriptions,
        post_call_required=False,
        call_session_store=store,
    )
    command = OutboundCallCommand(
        provider="vobiz",
        channel="phone",
        tenant_id="tenant-1",
        destination="+15551234567",
        idempotency_key="request-1",
    )

    session = await coordinator.start_outbound(command)

    assert session.metadata["call_status"] == "reconciliation_required"
    assert store.status == "reconciliation_required"
    assert subscriptions.released == []


@pytest.mark.asyncio
async def test_stale_call_reconciler_passes_configured_age_to_store():
    class _Store:
        age = None

        async def flag_stale_outbound_starts(self, older_than, limit=100):
            self.age = older_than
            return 2

    store = _Store()
    count = await reconcile_stale_outbound_once(store, older_than_seconds=90)

    assert count == 2
    assert store.age == timedelta(seconds=90)

