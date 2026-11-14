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
from chiefvoice.channels.vobiz.replay import SqlVobizCallbackNonceStore
from chiefvoice.identity.api_keys import SqlApiKeyDirectory
from chiefvoice.sessions import Direction


@pytest.mark.asyncio
async def test_postgres_tenant_scoping_billing_and_post_call_outbox():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("Set DATABASE_URL to run PostgreSQL integration tests")

    account_store = SqlAccountStore(database_url)
    billing = SqlBillingDirectory(database_url)
    queue = SqlPostCallQueue(database_url, Fernet.generate_key().decode("ascii"))
    vobiz_nonces = SqlVobizCallbackNonceStore(database_url)
    tenant_id = f"integration-{uuid4()}"
    other_tenant_id = f"integration-{uuid4()}"
    session_id = str(uuid4())
    try:
        await account_store.initialize()
        await billing.initialize()
        await queue.initialize()
        await vobiz_nonces.initialize()
        assert await vobiz_nonces.consume(tenant_id, "unique-provider-nonce")
        assert not await vobiz_nonces.consume(tenant_id, "unique-provider-nonce")
        assert await vobiz_nonces.consume(other_tenant_id, "unique-provider-nonce")

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
        await vobiz_nonces.close()
        await billing.close()
        await account_store.close()


@pytest.mark.asyncio
async def test_customer_provisioning_and_api_key_rotation_share_stable_tenant():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("Set DATABASE_URL to run PostgreSQL integration tests")

    api_keys = SqlApiKeyDirectory(database_url)
    account_store = SqlAccountStore(database_url)
    try:
        await api_keys.initialize()
        await account_store.initialize()
        account, initial_key, initial_record = await api_keys.provision_customer(
            display_name="Provisioned integration customer",
            billing_mode=BillingMode.PREPAID,
            created_by="platform-admin-test",
            key_name="primary",
            scopes=("calls:read", "calls:write"),
            expires_at=None,
        )
        assert await account_store.get(account.tenant_id) == account
        first_identity = await api_keys.authenticate(initial_key)
        assert first_identity is not None
        assert first_identity.tenant_id == account.tenant_id
        assert first_identity.key_id == initial_record.key_id

        rotated_key, rotated_record = await api_keys.create(
            tenant_id=account.tenant_id,
            name="rotation",
            scopes=("calls:read", "calls:write"),
            created_by="platform-admin-test",
            expires_at=None,
        )
        rotated_identity = await api_keys.authenticate(rotated_key)
        assert rotated_identity is not None
        assert rotated_identity.tenant_id == account.tenant_id
        assert rotated_identity.key_id == rotated_record.key_id
        assert await api_keys.revoke(account.tenant_id, initial_record.key_id)
        assert await api_keys.authenticate(initial_key) is None
        assert await api_keys.authenticate(rotated_key) is not None

        with pytest.raises(LookupError, match="Customer account does not exist"):
            await api_keys.create(
                tenant_id="missing-customer",
                name="orphan",
                scopes=("calls:read",),
                created_by="platform-admin-test",
                expires_at=None,
            )
    finally:
        await api_keys.close()
        await account_store.close()
