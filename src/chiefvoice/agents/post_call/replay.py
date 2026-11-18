from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from chiefvoice.agents.post_call.queue import (
    PostCallJobRow,
    PostCallWebhookConfigRow,
    PostCallWebhookDeliveryRow,
)
from chiefvoice.core.db_schema import verify_schema_revision


class ReplayError(Exception):
    """Base class for tenant-scoped replay failures."""


class ReplayNotFound(ReplayError):
    pass


class ReplayConflict(ReplayError):
    pass


class SqlPostCallReplayService:
    """Tenant-scoped retry transitions for failed post-call jobs and callbacks."""

    def __init__(self, database_url: str):
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def retry_analysis(self, tenant_id: str, session_id: str) -> dict[str, str]:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(PostCallJobRow)
                .where(
                    PostCallJobRow.tenant_id == tenant_id,
                    PostCallJobRow.session_id == session_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ReplayNotFound("Post-call job not found")
            if row.status == "queued":
                return {"session_id": session_id, "status": "queued"}
            if row.status != "failed":
                raise ReplayConflict(
                    f"Only failed analysis jobs can be retried (current: {row.status})"
                )
            row.status = "queued"
            row.attempts = 0
            row.next_attempt_at = now
            row.lease_expires_at = None
            row.last_error_type = None
            row.last_dispatch_error_type = None
            row.last_dispatched_at = None
            row.updated_at = now
        return {"session_id": session_id, "status": "queued"}

    async def retry_webhook(self, tenant_id: str, session_id: str) -> dict[str, str]:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            job = await session.scalar(
                select(PostCallJobRow)
                .where(
                    PostCallJobRow.tenant_id == tenant_id,
                    PostCallJobRow.session_id == session_id,
                    PostCallJobRow.status == "completed",
                    PostCallJobRow.encrypted_result.is_not(None),
                )
                .with_for_update()
            )
            if job is None:
                raise ReplayConflict("Completed post-call result is no longer available")
            delivery = await session.scalar(
                select(PostCallWebhookDeliveryRow)
                .where(
                    PostCallWebhookDeliveryRow.tenant_id == tenant_id,
                    PostCallWebhookDeliveryRow.session_id == session_id,
                    PostCallWebhookDeliveryRow.event_type == "call.analysis.completed",
                )
                .with_for_update()
            )
            if delivery is None:
                raise ReplayNotFound("Failed post-call webhook delivery not found")
            if delivery.status == "queued":
                return {"session_id": session_id, "status": "queued"}
            if delivery.status != "failed":
                raise ReplayConflict(
                    f"Only failed webhook deliveries can be retried (current: {delivery.status})"
                )

            config = await session.get(PostCallWebhookConfigRow, tenant_id)
            if config is None:
                raise ReplayConflict("Configure a post-call webhook before retrying this delivery")

            # Failed deliveries have their old target and secret erased. Retry against
            # the tenant's current config, preserving the event ID for receiver dedupe.
            delivery.target_url = config.target_url
            delivery.encrypted_secret = config.encrypted_secret
            delivery.status = "queued"
            delivery.attempts = 0
            delivery.next_attempt_at = now
            delivery.lease_expires_at = None
            delivery.last_status_code = None
            delivery.last_error_type = None
            delivery.delivered_at = None
            delivery.updated_at = now
        return {"session_id": session_id, "status": "queued"}
