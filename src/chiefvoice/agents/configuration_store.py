"""Tenant-scoped storage for agent provider configurations and credentials."""

from datetime import UTC, datetime

from cryptography.fernet import Fernet
from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from chiefvoice.accounts.store import Base
from chiefvoice.agents.configurations import AgentConfiguration
from chiefvoice.core.db_schema import verify_schema_revision


class AgentConfigurationRow(Base):
    __tablename__ = "agent_provider_configs"
    __table_args__ = (UniqueConstraint("tenant_id", "kind", "name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), ForeignKey("tenant_accounts.tenant_id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    location: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAgentConfigurationDirectory:
    def __init__(self, database_url: str, encryption_key: str | None):
        self._engine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._fernet = Fernet(encryption_key.encode("ascii")) if encryption_key else None

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    def _encrypt(self, value: str) -> str:
        if self._fernet is None:
            raise RuntimeError("CREDENTIALS_ENCRYPTION_KEY is required to store agent credentials")
        return self._fernet.encrypt(value.encode()).decode("ascii")

    def _decrypt(self, value: str) -> str:
        if self._fernet is None:
            raise RuntimeError("CREDENTIALS_ENCRYPTION_KEY is required to read agent credentials")
        return self._fernet.decrypt(value.encode("ascii")).decode()

    async def create(self, agent: AgentConfiguration) -> AgentConfiguration:
        now = datetime.now(UTC)
        row = AgentConfigurationRow(
            id=agent.agent_id, tenant_id=agent.tenant_id, name=agent.name,
            kind=agent.kind, provider=agent.provider, model=agent.model,
            location=agent.location, project_id=agent.project_id,
            encrypted_credentials=self._encrypt(agent.credentials_json or ""), enabled=True,
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
            row.project_id = agent.project_id
            if agent.credentials_json:
                row.encrypted_credentials = self._encrypt(agent.credentials_json)
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
            project_id=row.project_id, credentials_json=self._decrypt(row.encrypted_credentials),
            enabled=row.enabled, created_at=row.created_at, updated_at=row.updated_at,
        )
