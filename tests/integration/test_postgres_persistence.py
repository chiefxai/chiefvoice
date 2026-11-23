from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import select

from chiefvoice.accounts.models import new_tenant_account
from chiefvoice.accounts.store import SqlAccountStore
from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.agents.post_call.queue import SqlPostCallQueue
from chiefvoice.billing import BillingMode
from chiefvoice.billing.store import (
    BillingLedgerEntryRow,
    SqlBillingDirectory,
    TenantVoicePlanRow,
    VoiceReservationRow,
)
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
        assert await billing.mark_prepaid_voice_answered(
            tenant_id, session_id, answered_at
        ) == answered_at
        # Settlement returns whether this call exceeded the configured duration
        # limit (the caller uses it to emit the limit metric), not whether the
        # reservation was settled. Ten seconds is within the 60-second limit.
        assert not await billing.settle_prepaid_voice_at(
            tenant_id, session_id, 10, answered_at + timedelta(seconds=10)
        )
        usage = await billing.get_usage_for_api_key(tenant_id, None)
        assert usage is not None
        assert usage["billable_seconds"] == 10
        assert usage["charges_minor"] == 5
        assert usage["active_reservations"] == 0
        assert not await billing.settle_prepaid_voice_at(
            tenant_id, session_id, 10, answered_at + timedelta(seconds=10)
        )
        assert (await billing.get_plan(tenant_id))["prepaid_balance_minor"] == 95

        async with billing._sessions() as db:
            reservation = await db.get(VoiceReservationRow, session_id)
            assert reservation is not None
            assert reservation.tenant_id == tenant_id
            assert reservation.status == "settled"
            assert reservation.billable_seconds == 10
            assert reservation.charged_minor is not None

            plan = await db.get(TenantVoicePlanRow, tenant_id)
            assert plan is not None
            assert plan.prepaid_balance_minor == 100 - reservation.charged_minor

            ledger_entries = list(
                (
                    await db.scalars(
                        select(BillingLedgerEntryRow).where(
                            BillingLedgerEntryRow.session_id == session_id
                        )
                    )
                ).all()
            )
            assert len(ledger_entries) == 1
            assert ledger_entries[0].tenant_id == tenant_id
            assert ledger_entries[0].entry_type == "voice_charge"
            assert ledger_entries[0].amount_minor == -reservation.charged_minor

        # A separate over-limit call checks the event return semantics and
        # idempotent duration-limit marker independently.
        limit_session_id = str(uuid4())
        limit_entitlement = await billing.reserve_prepaid_voice(
            tenant_id, limit_session_id, "phone", Direction.OUTBOUND
        )
        assert limit_entitlement is not None and limit_entitlement.can_use_voice
        limit_answered_at = datetime.now(UTC)
        await billing.mark_prepaid_voice_answered(tenant_id, limit_session_id, limit_answered_at)
        assert await billing.settle_prepaid_voice_at(
            tenant_id, limit_session_id, 61, limit_answered_at + timedelta(seconds=61)
        )
        assert not await billing.settle_prepaid_voice_at(
            tenant_id, limit_session_id, 61, limit_answered_at + timedelta(seconds=61)
        )
        async with billing._sessions() as db:
            limit_reservation = await db.get(VoiceReservationRow, limit_session_id)
            assert limit_reservation is not None
            assert limit_reservation.tenant_id == tenant_id
            assert limit_reservation.status == "settled_over_limit"
            assert limit_reservation.duration_limit_event_reported is True
            assert await db.scalar(
                select(BillingLedgerEntryRow.entry_id).where(
                    BillingLedgerEntryRow.session_id == limit_session_id,
                    BillingLedgerEntryRow.tenant_id == other_tenant_id,
                )
            ) is None

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


def test_api_key_model_import_registers_agent_configuration_table_in_isolated_process():
    import os
    import subprocess
    import sys
    from pathlib import Path

    repository_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    source_path = str(repository_root / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        [source_path, env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import chiefvoice.identity.api_keys; "
            "from chiefvoice.accounts.store import Base; "
            "assert 'agent_provider_configs' in Base.metadata.tables; "
            "assert 'api_keys' in Base.metadata.tables",
        ],
        cwd=repository_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


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
