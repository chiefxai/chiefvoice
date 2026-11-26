"""PostgreSQL storage for tenant voice plans, reservations, and usage."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from math import ceil
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    JSON,
    MetaData,
    String,
    func,
    select,
    text,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from chiefvoice.billing.models import BillingMode, VoiceCapacityFull, VoiceEntitlement
from chiefvoice.core.db_schema import CONSTRAINT_NAMING, verify_schema_revision

logger = logging.getLogger(__name__)
UNANSWERED_RESERVATION_GRACE = timedelta(seconds=90)
ANSWERED_RESERVATION_GRACE = timedelta(seconds=30)
RECONCILIATION_BATCH_SIZE = 500


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=CONSTRAINT_NAMING)


class TenantVoicePlanRow(Base):
    __tablename__ = "tenant_voice_plans"

    tenant_id: Mapped[str] = mapped_column(
        String(200), primary_key=True
    )
    billing_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    voice_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate_per_minute_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    max_call_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    allowed_channels: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    max_concurrent_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    recording_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prepaid_balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    postpaid_credit_limit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    postpaid_used_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class VoiceReservationRow(Base):
    __tablename__ = "voice_session_reservations"

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    api_key_id: Mapped[str | None] = mapped_column(String(32), index=True)
    billing_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate_per_minute_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_call_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    billable_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    charged_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_limit_event_reported: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BillingLedgerEntryRow(Base):
    __tablename__ = "billing_ledger_entries"

    entry_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    api_key_id: Mapped[str | None] = mapped_column(String(32), index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    billing_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(40), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlBillingDirectory:
    """Directory and idempotent prepaid/postpaid ledgers for tenant voice plans."""

    def __init__(self, database_url: str):
        self._engine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def get_billing_mode(self, tenant_id: str) -> BillingMode | None:
        async with self._sessions() as session:
            row = await session.execute(
                text("SELECT billing_mode FROM tenant_accounts WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            mode = row.scalar_one_or_none()
            return BillingMode(mode) if mode else None

    async def configure(
        self,
        *,
        tenant_id: str,
        billing_mode: BillingMode,
        voice_enabled: bool,
        currency: str,
        rate_per_minute_minor: int,
        max_call_seconds: int,
        allowed_channels: list[str],
        max_concurrent_sessions: int,
        recording_allowed: bool,
        prepaid_balance_minor: int | None = None,
        postpaid_credit_limit_minor: int | None = None,
    ) -> dict:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            account_mode_result = await session.execute(
                text(
                    "SELECT billing_mode FROM tenant_accounts "
                    "WHERE tenant_id = :tenant_id FOR UPDATE"
                ),
                {"tenant_id": tenant_id},
            )
            account_mode = account_mode_result.scalar_one_or_none()
            if account_mode is None:
                raise LookupError("Tenant account was not found")
            if account_mode != billing_mode.value:
                raise ValueError("Billing mode differs from the tenant account mode")
            plan_mode = await session.scalar(
                select(TenantVoicePlanRow.billing_mode)
                .where(TenantVoicePlanRow.tenant_id == tenant_id)
                .with_for_update()
            )
            if plan_mode is not None and plan_mode != billing_mode.value:
                raise ValueError("Billing mode differs from the tenant account mode")

            existing = await session.scalar(
                select(TenantVoicePlanRow)
                .where(TenantVoicePlanRow.tenant_id == tenant_id)
                .with_for_update()
            )
            prepaid_balance = (
                prepaid_balance_minor
                if prepaid_balance_minor is not None
                else (existing.prepaid_balance_minor if existing else 0)
            )
            postpaid_limit = (
                postpaid_credit_limit_minor
                if postpaid_credit_limit_minor is not None
                else (existing.postpaid_credit_limit_minor if existing else 0)
            )
            if existing:
                if currency != existing.currency:
                    ledger_entries = await session.scalar(
                        select(func.count()).select_from(BillingLedgerEntryRow).where(
                            BillingLedgerEntryRow.tenant_id == tenant_id
                        )
                    )
                    if (
                        ledger_entries
                        or existing.prepaid_balance_minor
                        or existing.postpaid_used_minor
                        or existing.postpaid_credit_limit_minor
                    ):
                        raise ValueError("Currency cannot change after billing history exists")
                reserved = await session.scalar(
                    select(func.coalesce(func.sum(VoiceReservationRow.reserved_minor), 0)).where(
                        VoiceReservationRow.tenant_id == tenant_id,
                        VoiceReservationRow.status == "reserved",
                    )
                )
                current_balance = existing.prepaid_balance_minor
                if prepaid_balance_minor is not None and prepaid_balance < int(reserved or 0):
                    raise ValueError("Prepaid balance cannot be lowered below active reservations")
                if (
                    postpaid_credit_limit_minor is not None
                    and postpaid_limit < existing.postpaid_used_minor + int(reserved or 0)
                ):
                    raise ValueError("Credit limit cannot be lowered below used and reserved credit")
                if prepaid_balance_minor is not None and prepaid_balance != current_balance:
                    delta = prepaid_balance - current_balance
                    session.add(
                        BillingLedgerEntryRow(
                            entry_id=str(uuid4()), tenant_id=tenant_id, session_id=None,
                            billing_mode=billing_mode.value, currency=currency,
                            entry_type="admin_balance_adjustment", amount_minor=delta,
                            occurred_at=now,
                        )
                    )
                existing.billing_mode = billing_mode.value
                existing.voice_enabled = voice_enabled
                existing.currency = currency
                existing.rate_per_minute_minor = rate_per_minute_minor
                existing.max_call_seconds = max_call_seconds
                existing.allowed_channels = allowed_channels
                existing.max_concurrent_sessions = max_concurrent_sessions
                existing.recording_allowed = recording_allowed
                existing.prepaid_balance_minor = prepaid_balance
                existing.postpaid_credit_limit_minor = postpaid_limit
                existing.updated_at = now
                row = existing
            else:
                row = TenantVoicePlanRow(
                    tenant_id=tenant_id,
                    billing_mode=billing_mode.value,
                    voice_enabled=voice_enabled,
                    currency=currency,
                    rate_per_minute_minor=rate_per_minute_minor,
                    max_call_seconds=max_call_seconds,
                    allowed_channels=allowed_channels,
                    max_concurrent_sessions=max_concurrent_sessions,
                    recording_allowed=recording_allowed,
                    prepaid_balance_minor=prepaid_balance,
                    postpaid_credit_limit_minor=postpaid_limit,
                    postpaid_used_minor=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                if prepaid_balance:
                    session.add(
                        BillingLedgerEntryRow(
                            entry_id=str(uuid4()), tenant_id=tenant_id, session_id=None,
                            billing_mode=billing_mode.value, currency=currency,
                            entry_type="admin_initial_balance", amount_minor=prepaid_balance,
                            occurred_at=now,
                        )
                    )
        return self._serialize(row)

    async def get_plan(self, tenant_id: str) -> dict | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(TenantVoicePlanRow).where(TenantVoicePlanRow.tenant_id == tenant_id)
            )
            return self._serialize(row) if row else None

    async def get_usage_for_api_key(
        self, tenant_id: str, api_key_id: str, *,
        start_at: datetime | None = None, end_at: datetime | None = None,
    ) -> dict | None:
        async with self._sessions() as session:
            plan = await session.scalar(
                select(TenantVoicePlanRow).where(TenantVoicePlanRow.tenant_id == tenant_id)
            )
            if plan is None:
                return None
            usage_filters = [
                VoiceReservationRow.tenant_id == tenant_id,
                VoiceReservationRow.api_key_id == api_key_id,
            ]
            if start_at is not None:
                usage_filters.append(VoiceReservationRow.created_at >= start_at)
            if end_at is not None:
                usage_filters.append(VoiceReservationRow.created_at <= end_at)
            row = await session.execute(
                select(
                    func.count(VoiceReservationRow.session_id),
                    func.coalesce(func.sum(VoiceReservationRow.billable_seconds), 0),
                    func.coalesce(func.sum(VoiceReservationRow.charged_minor), 0),
                    func.count().filter(VoiceReservationRow.status == "reserved"),
                ).where(*usage_filters)
            )
            calls, seconds, charges, active = row.one()
            return {
                "billing_mode": plan.billing_mode,
                "currency": plan.currency,
                "rate_per_minute_minor": plan.rate_per_minute_minor,
                "calls": int(calls or 0),
                "billable_seconds": int(seconds or 0),
                "charges_minor": int(charges or 0),
                "active_reservations": int(active or 0),
                "customer_prepaid_balance_minor": plan.prepaid_balance_minor,
                "customer_postpaid_used_minor": plan.postpaid_used_minor,
                "customer_postpaid_credit_limit_minor": plan.postpaid_credit_limit_minor,
            }

    async def reserve_prepaid_voice(self, tenant_id, session_id, channel, direction, api_key_id=None):
        return await self._reserve(tenant_id, session_id, channel, direction, BillingMode.PREPAID, api_key_id)

    async def authorize_postpaid_voice(self, tenant_id, session_id, channel, direction, api_key_id=None):
        return await self._reserve(tenant_id, session_id, channel, direction, BillingMode.POSTPAID, api_key_id)

    async def _reserve(self, tenant_id, session_id, channel, direction, mode, api_key_id=None):
        now = datetime.now(UTC)
        await self.reconcile_expired_reservations(now=now)
        async with self._sessions.begin() as session:
            existing = await session.scalar(
                select(VoiceReservationRow)
                .where(VoiceReservationRow.session_id == session_id)
                .with_for_update()
            )
            if existing:
                if (
                    existing.tenant_id == tenant_id
                    and existing.api_key_id == api_key_id
                    and existing.status == "reserved"
                ):
                    if existing.billing_mode != mode.value or existing.channel != channel:
                        return None
                    plan = await session.scalar(
                        select(TenantVoicePlanRow).where(
                            TenantVoicePlanRow.tenant_id == tenant_id
                        )
                    )
                    if plan is None:
                        return None
                    return self._entitlement(
                        tenant_id, mode, channel, plan.voice_enabled,
                        plan.recording_allowed, plan.max_concurrent_sessions,
                        plan.max_call_seconds,
                    )
                return None
            plan = await session.scalar(
                select(TenantVoicePlanRow)
                .where(TenantVoicePlanRow.tenant_id == tenant_id)
                .with_for_update()
            )
            if (
                plan is None
                or plan.billing_mode != mode.value
                or not plan.voice_enabled
                or channel not in plan.allowed_channels
            ):
                return None
            active_count = await session.scalar(
                select(func.count()).select_from(VoiceReservationRow).where(
                    VoiceReservationRow.tenant_id == tenant_id,
                    VoiceReservationRow.status == "reserved",
                )
            )
            active_reserved = await session.scalar(
                select(func.coalesce(func.sum(VoiceReservationRow.reserved_minor), 0)).where(
                    VoiceReservationRow.tenant_id == tenant_id,
                    VoiceReservationRow.status == "reserved",
                )
            )
            reserve_minor = _cost_minor(plan.rate_per_minute_minor, plan.max_call_seconds)
            if mode is BillingMode.PREPAID:
                available = plan.prepaid_balance_minor - int(active_reserved or 0)
                total_available = plan.prepaid_balance_minor
            else:
                available = (
                    plan.postpaid_credit_limit_minor
                    - plan.postpaid_used_minor
                    - int(active_reserved or 0)
                )
                total_available = plan.postpaid_credit_limit_minor - plan.postpaid_used_minor
            if int(active_count or 0) >= plan.max_concurrent_sessions:
                if total_available < reserve_minor:
                    return None
                raise VoiceCapacityFull(tenant_id)
            if available < reserve_minor:
                return None
            session.add(
                VoiceReservationRow(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    api_key_id=api_key_id,
                    billing_mode=mode.value,
                    channel=channel,
                    direction=direction.value,
                    currency=plan.currency,
                    rate_per_minute_minor=plan.rate_per_minute_minor,
                    reserved_minor=reserve_minor,
                    max_call_seconds=plan.max_call_seconds,
                    status="reserved",
                    billable_seconds=None,
                    charged_minor=None,
                    answered_at=None,
                    duration_limit_event_reported=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            return self._entitlement(
                tenant_id, mode, channel, plan.voice_enabled, plan.recording_allowed,
                plan.max_concurrent_sessions, plan.max_call_seconds,
            )

    async def release_prepaid_voice(self, tenant_id: str, session_id: str) -> None:
        await self._release(tenant_id, session_id, BillingMode.PREPAID)

    async def release_postpaid_voice(self, tenant_id: str, session_id: str) -> None:
        await self._release(tenant_id, session_id, BillingMode.POSTPAID)

    async def _release(self, tenant_id: str, session_id: str, mode: BillingMode) -> None:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(VoiceReservationRow)
                .where(
                    VoiceReservationRow.session_id == session_id,
                    VoiceReservationRow.tenant_id == tenant_id,
                    VoiceReservationRow.billing_mode == mode.value,
                )
                .with_for_update()
            )
            if row and row.status == "reserved":
                row.status = "released"
                row.updated_at = datetime.now(UTC)

    async def mark_prepaid_voice_answered(
        self, tenant_id: str, session_id: str, answered_at: datetime
    ) -> datetime | None:
        return await self.mark_answered(tenant_id, session_id, answered_at)

    async def mark_postpaid_voice_answered(
        self, tenant_id: str, session_id: str, answered_at: datetime
    ) -> datetime | None:
        return await self.mark_answered(tenant_id, session_id, answered_at)

    async def mark_answered(
        self, tenant_id: str, session_id: str, answered_at: datetime
    ) -> datetime | None:
        timestamp = _as_utc(answered_at)
        async with self._sessions.begin() as session:
            reservation = await session.scalar(
                select(VoiceReservationRow)
                .where(
                    VoiceReservationRow.session_id == session_id,
                    VoiceReservationRow.tenant_id == tenant_id,
                    VoiceReservationRow.status == "reserved",
                )
                .with_for_update()
            )
            if reservation is None:
                raise LookupError("Voice call reservation is missing or no longer active")
            if reservation.status != "reserved":
                raise LookupError("Voice call reservation is missing or no longer active")
            if reservation.answered_at is None:
                reservation.answered_at = timestamp
                reservation.updated_at = datetime.now(UTC)
            return _as_utc(reservation.answered_at)

    async def reconcile_expired_reservations(
        self,
        *,
        now: datetime | None = None,
        batch_size: int = RECONCILIATION_BATCH_SIZE,
    ) -> dict[str, int]:
        """Release stale unanswered calls and cap-settle abandoned answered calls."""
        if batch_size < 1:
            raise ValueError("Reconciliation batch size must be positive")
        now = _as_utc(now or datetime.now(UTC))
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(VoiceReservationRow)
                    .where(VoiceReservationRow.status == "reserved")
                    .order_by(VoiceReservationRow.created_at)
                    .limit(batch_size)
                )
            ).all()
            candidates = [
                (
                    row.tenant_id,
                    row.session_id,
                    row.answered_at,
                    row.created_at,
                    row.max_call_seconds,
                    row.billing_mode,
                )
                for row in rows
            ]
        released = 0
        settled = 0
        limit_events = 0
        for tenant_id, session_id, answered_at, created_at, max_call_seconds, billing_mode in candidates:
            if answered_at is None:
                if now < _as_utc(created_at) + timedelta(seconds=max_call_seconds) + UNANSWERED_RESERVATION_GRACE:
                    continue
                if await self._release_expired_unanswered(tenant_id, session_id, now):
                    released += 1
                continue
            answered_at = _as_utc(answered_at)
            if now < answered_at + timedelta(seconds=max_call_seconds) + ANSWERED_RESERVATION_GRACE:
                continue
            cap_end = answered_at + timedelta(seconds=max_call_seconds)
            await self._settle(
                tenant_id,
                session_id,
                max_call_seconds,
                BillingMode(billing_mode),
                cap_end,
            )
            settled += 1
            if await self.record_duration_limit_event(tenant_id, session_id):
                from chiefvoice.observability.setup import call_duration_limit_event

                call_duration_limit_event()
                limit_events += 1
        return {
            "released_unanswered": released,
            "settled_answered_at_cap": settled,
            "duration_limit_events": limit_events,
        }

    async def _release_expired_unanswered(
        self, tenant_id: str, session_id: str, now: datetime
    ) -> bool:
        async with self._sessions.begin() as session:
            reservation = await session.scalar(
                select(VoiceReservationRow)
                .where(
                    VoiceReservationRow.tenant_id == tenant_id,
                    VoiceReservationRow.session_id == session_id,
                    VoiceReservationRow.status == "reserved",
                    VoiceReservationRow.answered_at.is_(None),
                )
                .with_for_update()
            )
            if reservation is None:
                return False
            expiry = (
                _as_utc(reservation.created_at)
                + timedelta(seconds=reservation.max_call_seconds)
                + UNANSWERED_RESERVATION_GRACE
            )
            if now < expiry:
                return False
            reservation.status = "released"
            reservation.updated_at = datetime.now(UTC)
            logger.warning(
                "stale_unanswered_voice_reservation_released",
                extra={"session_id": session_id, "tenant_id": tenant_id},
            )
            return True

    async def record_duration_limit_event(self, tenant_id: str, session_id: str) -> bool:
        async with self._sessions.begin() as session:
            reservation = await session.scalar(
                select(VoiceReservationRow)
                .where(
                    VoiceReservationRow.session_id == session_id,
                    VoiceReservationRow.tenant_id == tenant_id,
                )
                .with_for_update()
            )
            if reservation is None or reservation.duration_limit_event_reported:
                return False
            reservation.duration_limit_event_reported = True
            reservation.updated_at = datetime.now(UTC)
            return True

    async def mark_prepaid_voice_duration_limit_event(
        self, tenant_id: str, session_id: str
    ) -> bool:
        return await self.record_duration_limit_event(tenant_id, session_id)

    async def mark_postpaid_voice_duration_limit_event(
        self, tenant_id: str, session_id: str
    ) -> bool:
        return await self.record_duration_limit_event(tenant_id, session_id)

    async def settle_prepaid_voice(
        self, tenant_id: str, session_id: str, billable_seconds: int
    ) -> bool:
        return await self._settle(
            tenant_id, session_id, billable_seconds, BillingMode.PREPAID, None
        )

    async def accrue_postpaid_voice(
        self, tenant_id: str, session_id: str, billable_seconds: int
    ) -> bool:
        return await self._settle(
            tenant_id, session_id, billable_seconds, BillingMode.POSTPAID, None
        )

    async def settle_prepaid_voice_at(
        self, tenant_id: str, session_id: str, billable_seconds: int, ended_at: datetime
    ) -> bool:
        return await self._settle(
            tenant_id, session_id, billable_seconds, BillingMode.PREPAID, ended_at
        )

    async def accrue_postpaid_voice_at(
        self, tenant_id: str, session_id: str, billable_seconds: int, ended_at: datetime
    ) -> bool:
        return await self._settle(
            tenant_id, session_id, billable_seconds, BillingMode.POSTPAID, ended_at
        )

    async def _settle(
        self,
        tenant_id: str,
        session_id: str,
        billable_seconds: int,
        mode: BillingMode,
        ended_at: datetime | None,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            reservation = await session.scalar(
                select(VoiceReservationRow)
                .where(
                    VoiceReservationRow.session_id == session_id,
                    VoiceReservationRow.tenant_id == tenant_id,
                    VoiceReservationRow.billing_mode == mode.value,
                )
                .with_for_update()
            )
            if reservation is None or reservation.status != "reserved":
                return False
            plan = await session.scalar(
                select(TenantVoicePlanRow)
                .where(TenantVoicePlanRow.tenant_id == tenant_id)
                .with_for_update()
            )
            if plan is None:
                return False
            if reservation.answered_at is not None:
                finish = _as_utc(ended_at) if ended_at is not None else now
                seconds = max(
                    0,
                    ceil((finish - _as_utc(reservation.answered_at)).total_seconds()),
                )
            else:
                # Calls without an answer event have no billable talk time.
                seconds = 0
            amount, over_limit = _settlement_amount(
                reservation.rate_per_minute_minor,
                reservation.reserved_minor,
                reservation.max_call_seconds,
                seconds,
            )
            reservation.status = "settled_over_limit" if over_limit else "settled"
            reservation.billable_seconds = seconds
            reservation.charged_minor = amount
            reservation.updated_at = now
            emit_limit_event = over_limit and not reservation.duration_limit_event_reported
            if emit_limit_event:
                reservation.duration_limit_event_reported = True
            if mode is BillingMode.PREPAID:
                plan.prepaid_balance_minor -= amount
                entry_type = "voice_charge"
            else:
                plan.postpaid_used_minor += amount
                entry_type = "voice_accrual"
            plan.updated_at = now
            session.add(
                BillingLedgerEntryRow(
                    entry_id=str(uuid4()),
                    tenant_id=tenant_id,
                    api_key_id=reservation.api_key_id,
                    session_id=session_id,
                    billing_mode=mode.value,
                    currency=reservation.currency,
                    entry_type=f"{entry_type}_over_limit" if over_limit else entry_type,
                    amount_minor=-amount if mode is BillingMode.PREPAID else amount,
                    occurred_at=now,
                )
            )
            if over_limit:
                logger.error(
                    "voice_call_exceeded_max_duration",
                    extra={"session_id": session_id, "tenant_id": tenant_id},
                )
            return emit_limit_event

    @staticmethod
    def _entitlement(
        tenant_id, mode, channel, enabled, recording_allowed, max_concurrent=1,
        max_call_seconds=3_600,
    ):
        return VoiceEntitlement(
            tenant_id=tenant_id,
            billing_mode=mode,
            can_use_voice=enabled,
            allowed_channels=frozenset({channel}),
            max_concurrent_sessions=max_concurrent,
            recording_allowed=recording_allowed,
            max_call_seconds=max_call_seconds,
        )

    @staticmethod
    def _serialize(row: TenantVoicePlanRow) -> dict:
        return {
            "tenant_id": row.tenant_id,
            "billing_mode": row.billing_mode,
            "voice_enabled": row.voice_enabled,
            "currency": row.currency,
            "rate_per_minute_minor": row.rate_per_minute_minor,
            "max_call_seconds": row.max_call_seconds,
            "allowed_channels": row.allowed_channels,
            "max_concurrent_sessions": row.max_concurrent_sessions,
            "recording_allowed": row.recording_allowed,
            "prepaid_balance_minor": row.prepaid_balance_minor,
            "postpaid_credit_limit_minor": row.postpaid_credit_limit_minor,
            "postpaid_used_minor": row.postpaid_used_minor,
            "updated_at": row.updated_at.isoformat(),
        }


def _cost_minor(rate_per_minute_minor: int, seconds: int) -> int:
    """Round every partial billed minute up, using integer arithmetic only."""
    if seconds <= 0 or rate_per_minute_minor <= 0:
        return 0
    return (rate_per_minute_minor * seconds + 59) // 60


def _settlement_amount(
    rate_per_minute_minor: int,
    reserved_minor: int,
    max_call_seconds: int,
    billable_seconds: int,
) -> tuple[int, bool]:
    over_limit = billable_seconds > max_call_seconds
    capped_seconds = min(max(0, billable_seconds), max_call_seconds)
    return (
        min(reserved_minor, _cost_minor(rate_per_minute_minor, capped_seconds)),
        over_limit,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
