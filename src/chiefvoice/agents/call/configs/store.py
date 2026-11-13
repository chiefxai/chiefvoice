from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String, Text, UniqueConstraint, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from chiefvoice.accounts.store import Base
from chiefvoice.agents.call.configs.profiles import AgentProfile, SpeakingStyle
from chiefvoice.core.db_schema import verify_schema_revision


class AgentProfileRow(Base):
    __tablename__ = "agent_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    language: Mapped[str] = mapped_column(String(35), nullable=False)
    voice: Mapped[str] = mapped_column(String(80), nullable=False)
    speaking_style: Mapped[str] = mapped_column(String(20), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SqlAgentProfileDirectory:
    """PostgreSQL-backed agent profiles with tenant-filtered reads and writes."""

    def __init__(self, database_url: str):
        self._engine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(self, profile: AgentProfile) -> AgentProfile:
        now = datetime.now(UTC)
        row = AgentProfileRow(
            id=profile.agent_id,
            tenant_id=profile.tenant_id,
            name=profile.name,
            language=profile.language,
            voice=profile.voice,
            speaking_style=profile.speaking_style.value,
            instructions=profile.instructions,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        async with self._sessions.begin() as session:
            session.add(row)
            await session.flush()
        return _to_profile(row)

    async def get(self, tenant_id: str, agent_id: str) -> AgentProfile | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(AgentProfileRow).where(
                    AgentProfileRow.tenant_id == tenant_id,
                    AgentProfileRow.id == agent_id,
                    AgentProfileRow.enabled.is_(True),
                )
            )
            return _to_profile(row) if row is not None else None

    async def list_for_tenant(self, tenant_id: str) -> list[AgentProfile]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(AgentProfileRow)
                .where(AgentProfileRow.tenant_id == tenant_id)
                .order_by(AgentProfileRow.created_at.desc())
            )
            return [_to_profile(row) for row in rows]

    async def update(self, profile: AgentProfile) -> AgentProfile | None:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(AgentProfileRow).where(
                    AgentProfileRow.tenant_id == profile.tenant_id,
                    AgentProfileRow.id == profile.agent_id,
                    AgentProfileRow.enabled.is_(True),
                )
            )
            if row is None:
                return None
            row.name = profile.name
            row.language = profile.language
            row.voice = profile.voice
            row.speaking_style = profile.speaking_style.value
            row.instructions = profile.instructions
            row.updated_at = datetime.now(UTC)
            await session.flush()
            return _to_profile(row)

    async def archive(self, tenant_id: str, agent_id: str) -> bool:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(AgentProfileRow).where(
                    AgentProfileRow.tenant_id == tenant_id,
                    AgentProfileRow.id == agent_id,
                    AgentProfileRow.enabled.is_(True),
                )
            )
            if row is None:
                return False
            row.enabled = False
            row.updated_at = datetime.now(UTC)
            return True


def _to_profile(row: AgentProfileRow) -> AgentProfile:
    return AgentProfile(
        agent_id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        language=row.language,
        voice=row.voice,
        speaking_style=SpeakingStyle(row.speaking_style),
        instructions=row.instructions,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
