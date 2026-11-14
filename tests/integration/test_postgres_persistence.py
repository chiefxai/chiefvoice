from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest

from chiefvoice.accounts.models import new_tenant_account
from chiefvoice.accounts.store import SqlAccountStore
from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.agents.post_call.queue import SqlPostCallQueue
from chiefvoice.billing import BillingMode
from chiefvoice.billing.store import SqlBillingDirectory
from chiefvoice.sessions import Direction


@pytest.mark.asyncio
async def test_postgres_tenant_scoping_billing_and_post_call_outbox():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("Set DATABASE_URL to run PostgreSQL integration tests")

    account_store = SqlAccountStore(database_url)
    billing = SqlBillingDirectory(database_url)
    queue = SqlPostCallQueue(database_url, Fernet.generate_key().decode("ascii"))
    tenant_id = f"integration-{uuid4()}"
    other_tenant_id = f"integration-{uuid4()}"
    session_id = str(uuid4())
    try:
        await account_store.initialize()
        await billing.initialize()
        await queue.initialize()

        assert await account_store.create(
            new_tenant_account(tenant_id, "Integration tenant", BillingMode.PREPAID, "ci")
        )
        assert await account_store.create(
            new_tenant_account(other_tenant_id, "Other tenant", BillingMode.PREPAID, "ci")
        )
        await billing.configure(
            tenant_id=tenant_id,
            billing_mode=BillingMode.PREPAID,
            voice_enabled=True,
            currency="USD",
            rate_per_minute_minor=30,
            max_call_seconds=60,
            allowed_channels=["phone"],
            max_concurrent_sessions=1,
            recording_allowed=False,
            prepaid_balance_minor=100,
        )
        entitlement = await billing.reserve_prepaid_voice(
            tenant_id, session_id, "phone", Direction.OUTBOUND
        )
        assert entitlement is not None and entitlement.can_use_voice
        assert await billing.reserve_prepaid_voice(
            other_tenant_id, session_id, "phone", Direction.OUTBOUND
        ) is None

        answered_at = datetime.now(UTC)
        await billing.mark_prepaid_voice_answered(tenant_id, session_id, answered_at)
        assert await billing.settle_prepaid_voice_at(
            tenant_id, session_id, 10, answered_at + timedelta(seconds=10)
        )
        assert not await billing.settle_prepaid_voice_at(
            tenant_id, session_id, 10, answered_at + timedelta(seconds=10)
        )

        call = CompletedCall(
            session_id=str(uuid4()),
            tenant_id=tenant_id,
            provider="vobiz",
            channel="phone",
            direction="outbound",
            transcript="Integration-only transcript",
        )
        assert await queue.enqueue(call)
        assert not await queue.enqueue(call)
        assert (await queue.get_status(tenant_id, call.session_id))["status"] == "queued"
        assert await queue.get_status(other_tenant_id, call.session_id) is None
    finally:
        await queue.close()
        await billing.close()
        await account_store.close()
