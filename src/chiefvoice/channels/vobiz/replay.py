"""Durable nonce replay protection for Vobiz inbound callbacks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, String, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from chiefvoice.accounts.store import Base
from chiefvoice.core.db_schema import verify_schema_revision


class VobizCallbackNonce(Base):
    __tablename__ = "vobiz_callback_nonces"

    tenant_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlVobizCallbackNonceStore:
    """Atomically accept a provider nonce once per tenant for a bounded window."""

    def __init__(self, database_url: str, *, retention: timedelta = timedelta(hours=24)):
        if retention <= timedelta(0):
            raise ValueError("Vobiz callback nonce retention must be positive")
        self._retention = retention
        self._engine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def consume(self, tenant_id: str, nonce: str) -> bool:
        if not tenant_id or not nonce or len(nonce) > 128:
            return False
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            # A TTL bounds storage while ensuring a nonce cannot be replayed during
            # the provider's retry/replay window. The indexed cutoff keeps cleanup
            # proportional to expired entries.
            await session.execute(
                delete(VobizCallbackNonce).where(
                    VobizCallbackNonce.created_at < now - self._retention
                )
            )
            result = await session.execute(
                insert(VobizCallbackNonce)
                .values(tenant_id=tenant_id, nonce=nonce, created_at=now)
                .on_conflict_do_nothing(
                    index_elements=[VobizCallbackNonce.tenant_id, VobizCallbackNonce.nonce]
                )
            )
            return result.rowcount == 1

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()
