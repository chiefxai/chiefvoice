from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import DateTime, Index, Integer, MetaData, String, Text, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from chiefvoice.cloud_projects.models import TenantVertexProject
from chiefvoice.core.db_schema import CONSTRAINT_NAMING, verify_schema_revision
from chiefvoice.observability.setup import (
    VERTEX_PROJECT_PROVISIONING,
    VERTEX_PROJECT_PROVISIONING_DURATION,
)

logger = logging.getLogger(__name__)


class VertexProjectBase(DeclarativeBase):
    metadata = MetaData(naming_convention=CONSTRAINT_NAMING)


class TenantVertexProjectRow(VertexProjectBase):
    __tablename__ = "tenant_vertex_projects"
    __table_args__ = (
        Index(
            "ix_tenant_vertex_projects_status_next_attempt_at_lease_expires_at",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(30), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    budget_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TenantVertexProjectProvisioner(Protocol):
    def provision(self, tenant_id: str, project_id: str, display_name: str) -> str: ...


class SqlTenantVertexProjectDirectory:
    """Durable per-tenant project provisioning queue and project lookup."""

    def __init__(
        self,
        database_url: str,
        *,
        poll_interval: float = 2.0,
        lease_duration: timedelta = timedelta(minutes=15),
        max_attempts: int = 5,
    ):
        self._engine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self.poll_interval = poll_interval
        self.lease_duration = lease_duration
        self.max_attempts = max_attempts

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def request_provisioning(self, tenant_id: str, display_name: str) -> dict:
        now = datetime.now(UTC)
        digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:12]
        project_id = f"cv-{digest}-{secrets.token_hex(3)}"
        stmt = (
            insert(TenantVertexProjectRow)
            .values(
                tenant_id=tenant_id,
                project_id=project_id,
                display_name=display_name[:200],
                status="pending",
                attempts=0,
                next_attempt_at=now,
                lease_expires_at=None,
                last_error_type=None,
                budget_name=None,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[TenantVertexProjectRow.tenant_id])
        )
        async with self._sessions.begin() as session:
            await session.execute(stmt)
            row = await session.scalar(
                select(TenantVertexProjectRow).where(TenantVertexProjectRow.tenant_id == tenant_id)
            )
            return self._serialize(row)

    async def get(self, tenant_id: str) -> TenantVertexProject | None:
        async with self._sessions() as session:
            row = await session.get(TenantVertexProjectRow, tenant_id)
            return self._model(row) if row is not None else None

    async def get_ready_project_id(self, tenant_id: str) -> str | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(TenantVertexProjectRow).where(
                    TenantVertexProjectRow.tenant_id == tenant_id,
                    TenantVertexProjectRow.status == "ready",
                )
            )
            return row.project_id if row is not None else None

    async def retry(self, tenant_id: str) -> dict:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(TenantVertexProjectRow)
                .where(TenantVertexProjectRow.tenant_id == tenant_id)
                .with_for_update()
            )
            if row is None:
                raise LookupError("Tenant Vertex project was not requested")
            if row.status == "ready":
                return self._serialize(row)
            if row.status not in {"failed", "pending"}:
                raise RuntimeError("Tenant Vertex project provisioning is already in progress")
            row.status = "pending"
            row.attempts = 0
            row.next_attempt_at = now
            row.lease_expires_at = None
            row.last_error_type = None
            row.updated_at = now
            return self._serialize(row)

    async def provision_one(self, provisioner: TenantVertexProjectProvisioner) -> bool:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(TenantVertexProjectRow)
                .where(
                    or_(
                        (TenantVertexProjectRow.status == "pending")
                        & (TenantVertexProjectRow.next_attempt_at <= now),
                        (TenantVertexProjectRow.status == "provisioning")
                        & (TenantVertexProjectRow.lease_expires_at <= now),
                    )
                )
                .order_by(TenantVertexProjectRow.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return False
            if row.attempts >= self.max_attempts:
                row.status = "failed"
                row.lease_expires_at = None
                row.last_error_type = "provisioning_lease_expired"
                row.updated_at = now
                return True
            row.status = "provisioning"
            row.attempts += 1
            row.lease_expires_at = now + self.lease_duration
            row.next_attempt_at = None
            row.updated_at = now
            job = (row.tenant_id, row.project_id, row.display_name, row.attempts)
        try:
            with VERTEX_PROJECT_PROVISIONING_DURATION.time():
                budget_name = await asyncio.to_thread(provisioner.provision, *job[:3])
        except Exception as exc:
            VERTEX_PROJECT_PROVISIONING.labels("failed").inc()
            retry_at = datetime.now(UTC) + timedelta(seconds=min(2**job[3], 900))
            new_status = "pending" if job[3] < self.max_attempts else "failed"
            async with self._sessions.begin() as session:
                row = await session.get(TenantVertexProjectRow, job[0])
                if row is not None and row.status == "provisioning" and row.attempts == job[3]:
                    row.status = new_status
                    row.next_attempt_at = retry_at if new_status == "pending" else None
                    row.lease_expires_at = None
                    row.last_error_type = type(exc).__name__
                    row.updated_at = datetime.now(UTC)
            logger.error(
                "tenant_vertex_project_provisioning_failed",
                extra={"tenant_id": job[0], "project_id": job[1], "attempt": job[3],
                       "error_type": type(exc).__name__},
            )
            return True
        else:
            VERTEX_PROJECT_PROVISIONING.labels("ready").inc()
        async with self._sessions.begin() as session:
            row = await session.get(TenantVertexProjectRow, job[0])
            if row is not None and row.status == "provisioning" and row.attempts == job[3]:
                row.status = "ready"
                row.lease_expires_at = None
                row.last_error_type = None
                row.budget_name = budget_name
                row.updated_at = datetime.now(UTC)
        return True

    async def run_worker(self, provisioner: TenantVertexProjectProvisioner) -> None:
        while True:
            try:
                worked = await self.provision_one(provisioner)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("tenant_vertex_project_worker_failed", extra={"error_type": type(exc).__name__})
                worked = False
            if not worked:
                await asyncio.sleep(self.poll_interval)

    @staticmethod
    def _model(row: TenantVertexProjectRow) -> TenantVertexProject:
        return TenantVertexProject(
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            status=row.status,
            attempts=row.attempts,
            last_error_type=row.last_error_type,
            budget_name=row.budget_name,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _serialize(row: TenantVertexProjectRow) -> dict:
        return {
            "project_id": row.project_id,
            "status": row.status,
            "attempts": row.attempts,
            "last_error_type": row.last_error_type,
            "budget_name": row.budget_name,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
