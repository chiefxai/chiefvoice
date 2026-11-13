from datetime import UTC, datetime, timedelta

import pytest

from chiefvoice.billing.models import BillingMode
from chiefvoice.billing.store import (
    SqlBillingDirectory,
    TenantVoicePlanRow,
    VoiceReservationRow,
    _cost_minor,
    _settlement_amount,
)


def test_cost_is_rounded_up_in_integer_minor_units():
    assert _cost_minor(125, 0) == 0
    assert _cost_minor(125, 1) == 3
    assert _cost_minor(125, 60) == 125
    assert _cost_minor(125, 61) == 128


def test_overrun_charge_is_capped_to_reserved_maximum():
    assert _settlement_amount(120, 600, 300, 301) == (600, True)
    assert _settlement_amount(120, 600, 300, 250) == (500, False)


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


class _FakeSession:
    def __init__(self, reservation, plan):
        self.reservation = reservation
        self.plan = plan
        self.ledger_entries = []

    async def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is VoiceReservationRow:
            return self.reservation
        if entity is TenantVoicePlanRow:
            return self.plan
        raise AssertionError(f"Unexpected query for {entity}")

    def add(self, row):
        self.ledger_entries.append(row)


class _FakeSessionFactory:
    def __init__(self, session):
        self.session = session

    def begin(self):
        return _SessionContext(self.session)


@pytest.mark.asyncio
async def test_duplicate_settlement_is_idempotent_and_uses_answer_timestamp():
    answered_at = datetime(2026, 1, 1, tzinfo=UTC)
    ended_at = answered_at + timedelta(seconds=61)
    reservation = VoiceReservationRow(
        session_id="session",
        tenant_id="tenant",
        billing_mode=BillingMode.PREPAID.value,
        channel="phone",
        direction="outbound",
        currency="USD",
        rate_per_minute_minor=125,
        reserved_minor=1_000,
        max_call_seconds=300,
        status="reserved",
        billable_seconds=None,
        charged_minor=None,
        answered_at=answered_at,
        created_at=answered_at,
        updated_at=answered_at,
    )
    plan = TenantVoicePlanRow(
        tenant_id="tenant",
        billing_mode=BillingMode.PREPAID.value,
        voice_enabled=True,
        currency="USD",
        rate_per_minute_minor=125,
        max_call_seconds=300,
        allowed_channels=["phone"],
        max_concurrent_sessions=1,
        recording_allowed=False,
        prepaid_balance_minor=1_000,
        postpaid_credit_limit_minor=0,
        postpaid_used_minor=0,
        created_at=answered_at,
        updated_at=answered_at,
    )
    store = object.__new__(SqlBillingDirectory)
    fake_session = _FakeSession(reservation, plan)
    store._sessions = _FakeSessionFactory(fake_session)

    await store.settle_prepaid_voice_at("tenant", "session", 999, ended_at)
    await store.settle_prepaid_voice_at("tenant", "session", 999, ended_at)

    assert reservation.status == "settled"
    assert reservation.billable_seconds == 61
    assert reservation.charged_minor == 128
    assert plan.prepaid_balance_minor == 872
    assert len(fake_session.ledger_entries) == 1
    assert fake_session.ledger_entries[0].amount_minor == -128


@pytest.mark.asyncio
async def test_answer_time_is_first_write_wins_across_duplicate_callbacks():
    first_answer = datetime(2026, 1, 1, tzinfo=UTC)
    later_callback = first_answer + timedelta(seconds=15)
    reservation = VoiceReservationRow(
        session_id="session",
        tenant_id="tenant",
        billing_mode=BillingMode.PREPAID.value,
        channel="phone",
        direction="outbound",
        currency="USD",
        rate_per_minute_minor=100,
        reserved_minor=500,
        max_call_seconds=300,
        status="reserved",
        billable_seconds=None,
        charged_minor=None,
        answered_at=None,
        created_at=first_answer,
        updated_at=first_answer,
    )
    store = object.__new__(SqlBillingDirectory)
    store._sessions = _FakeSessionFactory(_FakeSession(reservation, None))

    assert await store.mark_answered("tenant", "session", first_answer) == first_answer
    assert await store.mark_answered("tenant", "session", later_callback) == first_answer
    assert reservation.answered_at == first_answer


@pytest.mark.asyncio
async def test_duration_limit_metric_is_claimed_once_per_reservation():
    answered_at = datetime(2026, 1, 1, tzinfo=UTC)
    reservation = VoiceReservationRow(
        session_id="session",
        tenant_id="tenant",
        billing_mode=BillingMode.PREPAID.value,
        channel="phone",
        direction="outbound",
        currency="USD",
        rate_per_minute_minor=100,
        reserved_minor=500,
        max_call_seconds=300,
        status="settled",
        billable_seconds=300,
        charged_minor=500,
        answered_at=answered_at,
        duration_limit_event_reported=False,
        created_at=answered_at,
        updated_at=answered_at,
    )
    store = object.__new__(SqlBillingDirectory)
    store._sessions = _FakeSessionFactory(_FakeSession(reservation, None))

    assert await store.record_duration_limit_event("tenant", "session") is True
    assert await store.record_duration_limit_event("tenant", "session") is False
    assert reservation.duration_limit_event_reported is True
