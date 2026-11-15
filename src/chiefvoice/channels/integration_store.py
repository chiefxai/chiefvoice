from __future__ import annotations

import json

from cryptography.fernet import Fernet
from sqlalchemy import Boolean, ForeignKeyConstraint, Integer, MetaData, String, Text, delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload

from chiefvoice.channels.integrations import (
    ProviderIntegration,
    ProviderPhoneNumber,
    RegisteredPhoneNumber,
)
from chiefvoice.core.db_schema import CONSTRAINT_NAMING, verify_schema_revision


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=CONSTRAINT_NAMING)


class IntegrationRow(Base):
    __tablename__ = "provider_integrations"
    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)
    settings_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    phone_numbers: Mapped[list[PhoneNumberRow]] = relationship(
        back_populates="integration", cascade="all, delete-orphan", lazy="selectin"
    )


class PhoneNumberRow(Base):
    __tablename__ = "provider_phone_numbers"
    provider: Mapped[str] = mapped_column(String(40), primary_key=True)
    number: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False)
    inbound_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    outbound_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    standard_channels: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unlimited_channels: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    __table_args__ = (
        ForeignKeyConstraint(
            ["provider", "tenant_id"],
            ["provider_integrations.provider", "provider_integrations.tenant_id"],
            ondelete="CASCADE",
        ),
    )
    integration: Mapped[IntegrationRow] = relationship(back_populates="phone_numbers")


class SqlProviderIntegrationStore:
    """Shared tenant integration directory. Provider secrets are encrypted at rest."""

    def __init__(self, database_url: str, encryption_key: str):
        self._engine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._fernet = Fernet(encryption_key.encode("ascii"))

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def get(self, provider: str, tenant_id: str) -> ProviderIntegration | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(IntegrationRow)
                .where(IntegrationRow.provider == provider, IntegrationRow.tenant_id == tenant_id)
                .options(selectinload(IntegrationRow.phone_numbers))
            )
            return self._to_integration(row) if row else None

    async def get_for_inbound_number(
        self, provider: str, number: str
    ) -> ProviderIntegration | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(IntegrationRow)
                .join(PhoneNumberRow)
                .where(
                    PhoneNumberRow.provider == provider,
                    PhoneNumberRow.number == number,
                    PhoneNumberRow.inbound_enabled.is_(True),
                )
                .options(selectinload(IntegrationRow.phone_numbers))
            )
            return self._to_integration(row) if row else None

    async def get_for_phone_number(self, provider: str, number: str) -> ProviderIntegration | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(IntegrationRow)
                .join(PhoneNumberRow)
                .where(PhoneNumberRow.provider == provider, PhoneNumberRow.number == number)
                .options(selectinload(IntegrationRow.phone_numbers))
            )
            return self._to_integration(row) if row else None

    async def list_phone_numbers(self) -> list[RegisteredPhoneNumber]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(PhoneNumberRow).order_by(
                    PhoneNumberRow.provider, PhoneNumberRow.number, PhoneNumberRow.tenant_id
                )
            )
            return [
                RegisteredPhoneNumber(
                    provider=row.provider,
                    tenant_id=row.tenant_id,
                    number=row.number,
                    inbound_enabled=row.inbound_enabled,
                    outbound_enabled=row.outbound_enabled,
                    agent_id=row.agent_id,
                    standard_channels=row.standard_channels,
                    unlimited_channels=row.unlimited_channels,
                    tags=tuple(json.loads(row.tags_json or "[]")),
                )
                for row in rows
            ]

    async def save(self, integration: ProviderIntegration) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(IntegrationRow, (integration.provider, integration.tenant_id))
            if row is None:
                row = IntegrationRow(provider=integration.provider, tenant_id=integration.tenant_id)
                session.add(row)
            row.encrypted_credentials = self._fernet.encrypt(
                json.dumps(integration.credentials).encode()
            ).decode()
            row.settings_json = json.dumps(integration.settings)
            await session.flush()
            await session.execute(
                delete(PhoneNumberRow).where(
                    PhoneNumberRow.provider == integration.provider,
                    PhoneNumberRow.tenant_id == integration.tenant_id,
                )
            )
            session.add_all(
                PhoneNumberRow(
                    provider=integration.provider,
                    tenant_id=integration.tenant_id,
                    number=item.number,
                    inbound_enabled=item.inbound_enabled,
                    outbound_enabled=item.outbound_enabled,
                    agent_id=item.agent_id,
                    standard_channels=item.standard_channels,
                    unlimited_channels=item.unlimited_channels,
                    tags_json=json.dumps(item.tags),
                )
                for item in integration.phone_numbers
            )

    async def delete(self, provider: str, tenant_id: str) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(IntegrationRow, (provider, tenant_id))
            if row is not None:
                await session.delete(row)

    def _to_integration(self, row: IntegrationRow) -> ProviderIntegration:
        credentials = json.loads(self._fernet.decrypt(row.encrypted_credentials.encode()))
        return ProviderIntegration(
            provider=row.provider,
            tenant_id=row.tenant_id,
            credentials=credentials,
            settings=json.loads(row.settings_json),
            phone_numbers=tuple(
                ProviderPhoneNumber(
                    p.number, p.inbound_enabled, p.outbound_enabled, p.agent_id,
                    p.standard_channels, p.unlimited_channels,
                    tuple(json.loads(p.tags_json or "[]")),
                )
                for p in row.phone_numbers
            ),
        )
