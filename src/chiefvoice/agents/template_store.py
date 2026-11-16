"""PostgreSQL persistence for platform-managed reusable agent templates."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from chiefvoice.accounts.store import Base
from chiefvoice.agents.templates import AgentTemplate, TemplateKind
from chiefvoice.core.db_schema import verify_schema_revision
from chiefvoice.identity.api_keys import ApiKeyRow


class AgentTemplateRow(Base):
    __tablename__ = "agent_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    tools_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="[]")
    variables_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    settings_json: Mapped[str] = mapped_column(Text, nullable=False, server_default="{}")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApiKeyAgentTemplateRow(Base):
    __tablename__ = "api_key_agent_templates"

    api_key_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True
    )
    template_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_templates.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class SqlAgentTemplateDirectory:
    def __init__(self, database_url: str):
        self._engine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def create(self, template: AgentTemplate) -> AgentTemplate:
        now = datetime.now(UTC)
        row = AgentTemplateRow(
            id=template.template_id,
            kind=template.kind,
            name=template.name,
            instructions=template.instructions,
            tools_json=json.dumps(template.tools, ensure_ascii=False),
            variables_json=json.dumps(template.variables, ensure_ascii=False),
            settings_json=json.dumps(template.settings, ensure_ascii=False),
            revision=1,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        async with self._sessions.begin() as session:
            session.add(row)
            await session.flush()
        return self._to_template(row)

    async def get(self, template_id: str, *, include_disabled: bool = False) -> AgentTemplate | None:
        async with self._sessions() as session:
            query = select(AgentTemplateRow).where(AgentTemplateRow.id == template_id)
            if not include_disabled:
                query = query.where(AgentTemplateRow.enabled.is_(True))
            row = await session.scalar(query)
            return self._to_template(row) if row else None

    async def list_all(self, kind: str | None = None) -> list[AgentTemplate]:
        async with self._sessions() as session:
            query = select(AgentTemplateRow).where(AgentTemplateRow.enabled.is_(True))
            if kind:
                query = query.where(AgentTemplateRow.kind == kind)
            rows = await session.scalars(query.order_by(AgentTemplateRow.updated_at.desc()))
            return [self._to_template(row) for row in rows]

    async def update(self, template_id: str, template: AgentTemplate) -> AgentTemplate | None:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(AgentTemplateRow).where(
                    AgentTemplateRow.id == template_id, AgentTemplateRow.enabled.is_(True)
                ).with_for_update()
            )
            if row is None:
                return None
            row.kind = template.kind
            row.name = template.name
            row.instructions = template.instructions
            row.tools_json = json.dumps(template.tools, ensure_ascii=False)
            row.variables_json = json.dumps(template.variables, ensure_ascii=False)
            row.settings_json = json.dumps(template.settings, ensure_ascii=False)
            row.revision += 1
            row.updated_at = datetime.now(UTC)
            await session.flush()
            return self._to_template(row)

    async def archive(self, template_id: str) -> bool:
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(AgentTemplateRow).where(
                    AgentTemplateRow.id == template_id, AgentTemplateRow.enabled.is_(True)
                ).with_for_update()
            )
            if row is None:
                return False
            row.enabled = False
            row.updated_at = datetime.now(UTC)
            await session.execute(
                delete(ApiKeyAgentTemplateRow).where(ApiKeyAgentTemplateRow.template_id == template_id)
            )
            return True

    async def set_api_assignments(
        self,
        api_key_id: str,
        calling_ids: list[str],
        post_call_ids: list[str],
        default_calling_id: str | None,
        default_post_call_id: str | None,
    ) -> bool:
        if len(set(calling_ids)) != len(calling_ids) or len(set(post_call_ids)) != len(post_call_ids):
            raise ValueError("Template assignments must be unique")
        if default_calling_id is not None and default_calling_id not in calling_ids:
            raise ValueError("Default calling template must be assigned to this API")
        if default_post_call_id is not None and default_post_call_id not in post_call_ids:
            raise ValueError("Default post-call template must be assigned to this API")
        async with self._sessions.begin() as session:
            api_key = await session.get(ApiKeyRow, api_key_id)
            if api_key is None or api_key.revoked_at is not None:
                return False
            ids = set(calling_ids + post_call_ids)
            templates = list(await session.scalars(select(AgentTemplateRow).where(
                AgentTemplateRow.id.in_(ids), AgentTemplateRow.enabled.is_(True)
            ))) if ids else []
            template_by_id = {item.id: item for item in templates}
            for template_id in calling_ids:
                if template_id not in template_by_id or template_by_id[template_id].kind != "calling":
                    raise ValueError("Calling template is missing, disabled, or has the wrong kind")
            for template_id in post_call_ids:
                if template_id not in template_by_id or template_by_id[template_id].kind != "post_call":
                    raise ValueError("Post-call template is missing, disabled, or has the wrong kind")
            await session.execute(delete(ApiKeyAgentTemplateRow).where(
                ApiKeyAgentTemplateRow.api_key_id == api_key_id
            ))
            session.add_all(
                ApiKeyAgentTemplateRow(
                    api_key_id=api_key_id,
                    template_id=template_id,
                    kind=kind,
                    is_default=template_id == default_id,
                )
                for kind, ids_for_kind, default_id in (
                    ("calling", calling_ids, default_calling_id),
                    ("post_call", post_call_ids, default_post_call_id),
                )
                for template_id in ids_for_kind
            )
            return True

    async def list_api_assignments(self, api_key_id: str) -> dict[str, Any]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(ApiKeyAgentTemplateRow, AgentTemplateRow)
                .join(AgentTemplateRow, AgentTemplateRow.id == ApiKeyAgentTemplateRow.template_id)
                .where(ApiKeyAgentTemplateRow.api_key_id == api_key_id)
                .order_by(AgentTemplateRow.name)
            )
            items = rows.all()
        return {
            "calling": [self._to_template(template) for assignment, template in items if assignment.kind == "calling"],
            "post_call": [self._to_template(template) for assignment, template in items if assignment.kind == "post_call"],
            "default_calling_id": next((assignment.template_id for assignment, _ in items if assignment.kind == "calling" and assignment.is_default), None),
            "default_post_call_id": next((assignment.template_id for assignment, _ in items if assignment.kind == "post_call" and assignment.is_default), None),
        }

    async def resolve_for_api(
        self, api_key_id: str, kind: TemplateKind, requested_id: str | None
    ) -> AgentTemplate | None:
        async with self._sessions() as session:
            query = (
                select(AgentTemplateRow)
                .join(ApiKeyAgentTemplateRow, ApiKeyAgentTemplateRow.template_id == AgentTemplateRow.id)
                .where(
                    ApiKeyAgentTemplateRow.api_key_id == api_key_id,
                    ApiKeyAgentTemplateRow.kind == kind,
                    AgentTemplateRow.kind == kind,
                    AgentTemplateRow.enabled.is_(True),
                )
            )
            if requested_id:
                query = query.where(AgentTemplateRow.id == requested_id)
            else:
                query = query.where(ApiKeyAgentTemplateRow.is_default.is_(True))
            row = await session.scalar(query)
            return self._to_template(row) if row else None

    @staticmethod
    def _to_template(row: AgentTemplateRow) -> AgentTemplate:
        return AgentTemplate(
            template_id=row.id,
            kind=row.kind,  # type: ignore[arg-type]
            name=row.name,
            instructions=row.instructions,
            tools=tuple(json.loads(row.tools_json or "[]")),
            variables=json.loads(row.variables_json or "{}"),
            settings=json.loads(row.settings_json or "{}"),
            revision=row.revision,
            enabled=row.enabled,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
