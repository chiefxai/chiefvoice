"""Tenant-scoped storage for agent provider configurations and credentials."""

from datetime import UTC, datetime

import json

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from chiefvoice.accounts.store import Base
from chiefvoice.agents.configurations import AgentConfiguration
from chiefvoice.core.db_schema import verify_schema_revision


class AgentConfigurationRow(Base):
    __tablename__ = "agent_provider_configs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), ForeignKey("tenant_accounts.tenant_id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    location: Mapped[str] = mapped_column(String(64), nullable=False)
    cost_per_minute_minor: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR", server_default="INR")
    auto_hangup_when_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")


class SqlAgentConfigurationDirectory:
    def __init__(self, database_url: str):
        self._engine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(self, agent: AgentConfiguration) -> AgentConfiguration:
        now = datetime.now(UTC)
        row = AgentConfigurationRow(
            id=agent.agent_id, tenant_id=agent.tenant_id, name=agent.name,
            kind=agent.kind, provider=agent.provider, model=agent.model,
            location=agent.location, cost_per_minute_minor=agent.cost_per_minute_minor,
            currency=agent.currency, enabled=True,
            auto_hangup_when_complete=agent.auto_hangup_when_complete,
            tags_json=json.dumps(agent.tags),
            created_at=now, updated_at=now,
        )
        async with self._sessions.begin() as session:
            session.add(row)
            await session.flush()
        return self._to_agent(row)

    async def get(self, tenant_id: str, agent_id: str) -> AgentConfiguration | None:
        async with self._sessions() as session:
            row = await session.scalar(select(AgentConfigurationRow).where(
                AgentConfigurationRow.tenant_id == tenant_id,
                AgentConfigurationRow.id == agent_id,
                AgentConfigurationRow.enabled.is_(True),
            ))
            return self._to_agent(row) if row else None

    async def get_by_id(self, agent_id: str) -> AgentConfiguration | None:
        async with self._sessions() as session:
            row = await session.get(AgentConfigurationRow, agent_id)
            return self._to_agent(row) if row and row.enabled else None

    async def list_all(self) -> list[AgentConfiguration]:
        async with self._sessions() as session:
            rows = await session.scalars(select(AgentConfigurationRow).where(
                AgentConfigurationRow.enabled.is_(True),
            ).order_by(AgentConfigurationRow.created_at.desc()))
            return [self._to_agent(row) for row in rows]

    async def update(self, agent: AgentConfiguration) -> AgentConfiguration | None:
        async with self._sessions.begin() as session:
            row = await session.scalar(select(AgentConfigurationRow).where(
                AgentConfigurationRow.tenant_id == agent.tenant_id,
                AgentConfigurationRow.id == agent.agent_id,
                AgentConfigurationRow.enabled.is_(True),
            ))
            if row is None:
                return None
            row.name = agent.name
            row.kind = agent.kind
            row.provider = agent.provider
            row.model = agent.model
            row.location = agent.location
            row.cost_per_minute_minor = agent.cost_per_minute_minor
            row.currency = agent.currency
            row.auto_hangup_when_complete = agent.auto_hangup_when_complete
            row.tags_json = json.dumps(agent.tags)
            row.updated_at = datetime.now(UTC)
            await session.flush()
            return self._to_agent(row)

    async def archive(self, tenant_id: str, agent_id: str) -> bool:
        async with self._sessions.begin() as session:
            row = await session.scalar(select(AgentConfigurationRow).where(
                AgentConfigurationRow.tenant_id == tenant_id,
                AgentConfigurationRow.id == agent_id,
                AgentConfigurationRow.enabled.is_(True),
            ))
            if row is None:
                return False
            row.enabled = False
            row.updated_at = datetime.now(UTC)
            return True

    def _to_agent(self, row: AgentConfigurationRow) -> AgentConfiguration:
        return AgentConfiguration(
            agent_id=row.id, tenant_id=row.tenant_id, name=row.name, kind=row.kind,
            provider=row.provider, model=row.model, location=row.location,
            cost_per_minute_minor=row.cost_per_minute_minor, currency=row.currency,
            auto_hangup_when_complete=row.auto_hangup_when_complete,
            enabled=row.enabled, created_at=row.created_at, updated_at=row.updated_at,
            tags=tuple(json.loads(row.tags_json or "[]")),
        )
