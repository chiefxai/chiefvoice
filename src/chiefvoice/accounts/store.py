from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, String, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from chiefvoice.accounts.models import TenantAccount
from chiefvoice.billing import BillingMode
from chiefvoice.core.db_schema import CONSTRAINT_NAMING, verify_schema_revision


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=CONSTRAINT_NAMING)


class TenantAccountRow(Base):
    __tablename__ = "tenant_accounts"
    tenant_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    billing_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAccountStore:
    def __init__(self, database_url: str):
        self._engine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(self, account: TenantAccount) -> bool:
        async with self._sessions() as session:
            result = await session.execute(
                insert(TenantAccountRow)
                .values(
                    tenant_id=account.tenant_id,
                    display_name=account.display_name,
                    billing_mode=account.billing_mode.value,
                    created_by=account.created_by,
                    created_at=account.created_at,
                )
                .on_conflict_do_nothing(index_elements=[TenantAccountRow.tenant_id])
            )
            await session.commit()
            return result.rowcount == 1

    async def get(self, tenant_id: str) -> TenantAccount | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(TenantAccountRow).where(TenantAccountRow.tenant_id == tenant_id)
            )
            if row is None:
                return None
            created_at = row.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            return TenantAccount(
                tenant_id=row.tenant_id,
                display_name=row.display_name,
                billing_mode=BillingMode(row.billing_mode),
                created_by=row.created_by,
                created_at=created_at,
            )

    async def update_display_name(self, tenant_id: str, display_name: str) -> bool:
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(TenantAccountRow)
                .where(TenantAccountRow.tenant_id == tenant_id)
                .values(display_name=display_name)
            )
            return result.rowcount == 1

    async def list(self, *, limit: int = 100, offset: int = 0) -> list[TenantAccount]:
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("Account list pagination is out of range")
        async with self._sessions() as session:
            rows = await session.scalars(
                select(TenantAccountRow)
                .order_by(TenantAccountRow.created_at.desc(), TenantAccountRow.tenant_id)
                .limit(limit)
                .offset(offset)
            )
            return [
                TenantAccount(
                    tenant_id=row.tenant_id,
                    display_name=row.display_name,
                    billing_mode=BillingMode(row.billing_mode),
                    created_by=row.created_by,
                    created_at=(
                        row.created_at.replace(tzinfo=UTC)
                        if row.created_at.tzinfo is None
                        else row.created_at
                    ),
                )
                for row in rows
            ]

    async def get_billing_mode(self, tenant_id: str) -> BillingMode | None:
        account = await self.get(tenant_id)
        return account.billing_mode if account is not None else None
