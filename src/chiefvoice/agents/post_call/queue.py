from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
import traceback
import urllib.parse
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Protocol

from cryptography.fernet import Fernet
import httpx
from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    delete,
    exists,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from chiefvoice.agents.post_call.base import CompletedCall, PostCallAgent
from chiefvoice.core.db_schema import CONSTRAINT_NAMING, verify_schema_revision
from chiefvoice.data_lifecycle.retention import RetentionClass
from chiefvoice.observability.setup import (
    post_call_job_event,
    post_call_webhook_event,
    set_post_call_queue_metrics,
)

logger = logging.getLogger(__name__)
MAX_WEBHOOK_BODY_BYTES = 64 * 1024

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=CONSTRAINT_NAMING)


class PostCallJobRow(Base):
    __tablename__ = "post_call_jobs"
    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_dispatch_error_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_error_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    encrypted_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PostCallWebhookConfigRow(Base):
    __tablename__ = "post_call_webhook_configs"
    tenant_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PostCallWebhookDeliveryRow(Base):
    __tablename__ = "post_call_webhook_deliveries"
    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_secret: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


Index(
    "ix_post_call_jobs_status_lease_expires_at_created_at",
    PostCallJobRow.status,
    PostCallJobRow.lease_expires_at,
    PostCallJobRow.created_at,
)
Index(
    "ix_pcwh_status_lease_next_attempt",
    PostCallWebhookDeliveryRow.status,
    PostCallWebhookDeliveryRow.lease_expires_at,
    PostCallWebhookDeliveryRow.next_attempt_at,
)


class PostCallWebhookDirectory(Protocol):
    async def configure(self, tenant_id: str, target_url: str) -> tuple[str, datetime]: ...
    async def get(self, tenant_id: str) -> dict | None: ...
    async def delete(self, tenant_id: str) -> bool: ...


class SqlPostCallWebhookDirectory:
    """Tenant-scoped callback config; the signing key is only returned on creation."""

    def __init__(self, database_url: str, encryption_key: str):
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._fernet = Fernet(encryption_key.encode("ascii"))

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def configure(self, tenant_id: str, target_url: str) -> tuple[str, datetime]:
        await validate_public_webhook_url(target_url)
        secret = secrets.token_urlsafe(32)
        encrypted_secret = self._fernet.encrypt(secret.encode("ascii")).decode("ascii")
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            stmt = (
                insert(PostCallWebhookConfigRow)
                .values(
                    tenant_id=tenant_id,
                    target_url=target_url,
                    encrypted_secret=encrypted_secret,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[PostCallWebhookConfigRow.tenant_id],
                    set_={
                        "target_url": target_url,
                        "encrypted_secret": encrypted_secret,
                        "updated_at": now,
                    },
                )
            )
            await session.execute(stmt)
            cancelled = await session.execute(
                update(PostCallWebhookDeliveryRow)
                .where(
                    PostCallWebhookDeliveryRow.tenant_id == tenant_id,
                    PostCallWebhookDeliveryRow.status.in_(("queued", "processing")),
                )
                .values(
                    status="cancelled",
                    target_url="",
                    encrypted_secret="",
                    next_attempt_at=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
        if cancelled.rowcount:
            post_call_webhook_event("cancelled")
        return secret, now

    async def get(self, tenant_id: str) -> dict | None:
        async with self._sessions() as session:
            row = await session.get(PostCallWebhookConfigRow, tenant_id)
            if row is None:
                return None
            return {
                "target_url": row.target_url,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }

    async def delete(self, tenant_id: str) -> bool:
        cancelled_count = 0
        async with self._sessions.begin() as session:
            row = await session.get(PostCallWebhookConfigRow, tenant_id)
            if row is None:
                return False
            await session.delete(row)
            now = datetime.now(UTC)
            cancelled = await session.execute(
                update(PostCallWebhookDeliveryRow)
                .where(
                    PostCallWebhookDeliveryRow.tenant_id == tenant_id,
                    PostCallWebhookDeliveryRow.status.in_(("queued", "processing")),
                )
                .values(
                    status="cancelled",
                    target_url="",
                    encrypted_secret="",
                    next_attempt_at=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            cancelled_count = cancelled.rowcount or 0
        if cancelled_count:
            post_call_webhook_event("cancelled")
        return True


class PostCallDispatcher(Protocol):
    async def enqueue(self, call: CompletedCall) -> bool:
        """Persist a completed call for processing; return false when already queued."""
        ...

    async def get_status(self, tenant_id: str, session_id: str) -> dict | None: ...


class SqlPostCallQueue:
    """Encrypted PostgreSQL outbox plus a polling worker for post-call analysis."""

    data_class = RetentionClass.TRANSCRIPT

    def __init__(
        self,
        database_url: str,
        encryption_key: str,
        *,
        poll_interval: float = 1.0,
        lease_duration: timedelta = timedelta(minutes=5),
        max_attempts: int = 5,
    ):
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._fernet = Fernet(encryption_key.encode("ascii"))
        self.poll_interval = poll_interval
        self.lease_duration = lease_duration
        self.max_attempts = max_attempts
        self._last_metrics_refresh = 0.0

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def refresh_queue_metrics_if_due(self, *, refresh_interval: float = 15.0) -> None:
        """Refresh tenant-free backlog gauges from Postgres at a bounded interval."""
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_metrics_refresh < refresh_interval:
            return
        now = datetime.now(UTC)
        async with self._sessions() as session:
            rows = await session.execute(
                select(
                    PostCallJobRow.status,
                    func.count(),
                    func.min(PostCallJobRow.created_at),
                )
                .where(PostCallJobRow.status.in_(("queued", "processing", "failed")))
                .group_by(PostCallJobRow.status)
            )
            counts: dict[str, int] = {}
            oldest_queued_at = None
            for status, count, oldest_at in rows:
                counts[status] = count
                if status == "queued":
                    oldest_queued_at = oldest_at
        oldest_age = (
            max(0.0, (now - oldest_queued_at).total_seconds()) if oldest_queued_at else 0.0
        )
        set_post_call_queue_metrics(counts, oldest_age)
        self._last_metrics_refresh = now_monotonic

    async def enqueue(self, call: CompletedCall) -> bool:
        if not call.transcript or not call.transcript.strip():
            return False
        created_at = datetime.now(UTC)
        payload = {
            "transcript": call.transcript,
            "recording_uri": call.recording_uri,
            "post_call_context": call.post_call_context,
            "output_schema": call.output_schema,
            "tools": call.tools,
            "time_zone": call.time_zone,
            "time_format": call.time_format,
            "live_agent_context": call.live_agent_context,
            "api_key_id": call.api_key_id,
        }
        encrypted_payload = self._fernet.encrypt(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        stmt = (
            insert(PostCallJobRow)
            .values(
                session_id=call.session_id,
                tenant_id=call.tenant_id,
                provider=call.provider,
                channel=call.channel,
                direction=call.direction,
                started_at=_parse_datetime(call.started_at),
                ended_at=_parse_datetime(call.ended_at) or created_at,
                encrypted_payload=encrypted_payload,
                status="queued",
                attempts=0,
                next_attempt_at=created_at,
                lease_expires_at=None,
                last_error_type=None,
                encrypted_result=None,
                created_at=created_at,
                updated_at=created_at,
            )
            .on_conflict_do_nothing(index_elements=[PostCallJobRow.session_id])
        )
        async with self._sessions.begin() as session:
            result = await session.execute(stmt)
            inserted = result.rowcount == 1
        if inserted:
            post_call_job_event("enqueued")
        return inserted

    async def get_status(self, tenant_id: str, session_id: str) -> dict | None:
        """Return tenant-scoped job status and decrypted analysis, never the transcript."""
        async with self._sessions() as session:
            row = await session.scalar(
                select(PostCallJobRow).where(
                    PostCallJobRow.tenant_id == tenant_id,
                    PostCallJobRow.session_id == session_id,
                )
            )
            if row is None:
                return None
            result = None
            payload = json.loads(self._fernet.decrypt(row.encrypted_payload.encode("ascii")))
            if row.status == "completed" and row.encrypted_result:
                result = json.loads(self._fernet.decrypt(row.encrypted_result.encode("ascii")))
            delivery = await session.scalar(
                select(PostCallWebhookDeliveryRow).where(
                    PostCallWebhookDeliveryRow.tenant_id == tenant_id,
                    PostCallWebhookDeliveryRow.session_id == session_id,
                )
            )
            return {
                "session_id": row.session_id,
                "status": row.status,
                "attempts": row.attempts,
                "last_error_type": row.last_error_type,
                "result": result,
                "time_zone": payload.get("time_zone", "UTC"),
                "time_format": payload.get("time_format", "%Y-%m-%dT%H:%M:%S%z"),
                "webhook_status": delivery.status if delivery else None,
                "webhook_attempts": delivery.attempts if delivery else 0,
            }

    async def purge_expired(self, tenant_id, cutoff, batch_size: int) -> int:
        """Delete expired terminal jobs after pending analysis and webhook work completes.

        A tenant-wide transcript policy legal hold is enforced by `RetentionRunner`
        before this target is called. Queued/processing jobs and jobs with queued or
        processing webhook deliveries are never purged, even when past the cutoff.
        Failed analysis and terminal webhook delivery records remain until their
        configured retention cutoff, after which their transcript/result is removed.
        """
        if not tenant_id or cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("Tenant and timezone-aware cutoff are required")
        if batch_size < 1:
            raise ValueError("Purge batch size must be positive")
        async with self._sessions.begin() as session:
            result = await session.scalars(
                select(PostCallJobRow.session_id)
                .where(
                    PostCallJobRow.tenant_id == tenant_id,
                    PostCallJobRow.ended_at <= cutoff,
                    PostCallJobRow.status.in_(("completed", "failed")),
                    ~exists(
                        select(PostCallWebhookDeliveryRow.event_id).where(
                            PostCallWebhookDeliveryRow.tenant_id == tenant_id,
                            PostCallWebhookDeliveryRow.session_id == PostCallJobRow.session_id,
                            PostCallWebhookDeliveryRow.status.in_(("queued", "processing")),
                        )
                    ),
                )
                .order_by(PostCallJobRow.ended_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            session_ids = list(result.all())
            if not session_ids:
                return 0
            await session.execute(
                delete(PostCallWebhookDeliveryRow).where(
                    PostCallWebhookDeliveryRow.tenant_id == tenant_id,
                    PostCallWebhookDeliveryRow.session_id.in_(session_ids),
                )
            )
            await session.execute(
                delete(PostCallJobRow).where(
                    PostCallJobRow.tenant_id == tenant_id,
                    PostCallJobRow.session_id.in_(session_ids),
                )
            )
            return len(session_ids)

    async def run_worker(self, agent: PostCallAgent) -> None:
        """Poll durable jobs until cancelled; safe for multiple workers via SKIP LOCKED."""
        while True:
            try:
                await self.refresh_queue_metrics_if_due()
                analysis_worked = await self.process_one(agent)
                delivery_worked = await self.process_delivery_one()
                worked = analysis_worked or delivery_worked
            except Exception as exc:
                post_call_job_event("worker_iteration_failed")
                logger.error(
                    "post_call_worker_iteration_failed",
                    extra={"error_type": type(exc).__name__},
                )
                await asyncio.sleep(self.poll_interval)
                continue
            if not worked:
                await asyncio.sleep(self.poll_interval)

    async def run_async_dispatcher(self, invoker, *, redispatch_after: timedelta) -> None:
        """Publish durable job IDs to an async compute target; PostgreSQL remains authoritative."""
        while True:
            try:
                await self.refresh_queue_metrics_if_due()
                worked = await self.dispatch_one(invoker, redispatch_after=redispatch_after)
                delivery_worked = await self.process_delivery_one()
                if not worked and not delivery_worked:
                    await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                post_call_job_event("dispatcher_iteration_failed")
                logger.error("post_call_dispatcher_iteration_failed", extra={"error_type": type(exc).__name__})
                await asyncio.sleep(self.poll_interval)

    async def run_lambda_dispatcher(self, invoker, *, redispatch_after: timedelta) -> None:
        """Compatibility wrapper for deployments using the AWS Lambda adapter."""
        await self.run_async_dispatcher(invoker, redispatch_after=redispatch_after)

    async def dispatch_one(self, invoker, *, redispatch_after: timedelta) -> bool:
        """Send one eligible job ID. Old unclaimed jobs are resent after the recovery window."""
        now = datetime.now(UTC)
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(PostCallJobRow)
                    .where(
                        or_(
                            (PostCallJobRow.status == "queued")
                            & (PostCallJobRow.next_attempt_at <= now),
                            (PostCallJobRow.status == "processing")
                            & (PostCallJobRow.lease_expires_at <= now),
                        ),
                        or_(PostCallJobRow.last_dispatched_at.is_(None),
                            PostCallJobRow.last_dispatched_at <= now - redispatch_after),
                    )
                    .order_by(PostCallJobRow.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    return False
                job_id = row.session_id
                row.last_dispatched_at = now
                row.dispatch_attempts += 1
                dispatch_attempt = row.dispatch_attempts
                row.last_dispatch_error_type = None
                row.updated_at = now
        try:
            await invoker.invoke(job_id)
        except Exception as exc:
            retry_at = datetime.now(UTC) + timedelta(seconds=min(2**dispatch_attempt, 300))
            async with self._sessions.begin() as session:
                await session.execute(
                    update(PostCallJobRow)
                    .where(PostCallJobRow.session_id == job_id, PostCallJobRow.status == "queued")
                    .values(
                        last_dispatched_at=None,
                        last_dispatch_error_type=type(exc).__name__,
                        next_attempt_at=retry_at,
                        updated_at=datetime.now(UTC),
                    )
                )
            logger.error(
                "post_call_async_dispatch_failed",
                extra={"session_id": job_id, "error_type": type(exc).__name__},
            )
            post_call_job_event("dispatch_failed")
            return True
        post_call_job_event("dispatched")
        return True

    async def process_one(self, agent: PostCallAgent, session_id: str | None = None) -> bool:
        now = datetime.now(UTC)
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(PostCallJobRow)
                    .where(
                        *((PostCallJobRow.session_id == session_id,) if session_id is not None else ()),
                        or_(
                            (PostCallJobRow.status == "queued")
                            & (PostCallJobRow.next_attempt_at <= now),
                            (PostCallJobRow.status == "processing")
                            & (PostCallJobRow.lease_expires_at <= now),
                        )
                    )
                    .order_by(PostCallJobRow.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    return False
                if row.attempts >= self.max_attempts:
                    row.status = "failed"
                    row.lease_expires_at = None
                    row.next_attempt_at = None
                    row.last_error_type = "worker_lease_expired"
                    row.updated_at = now
                    post_call_job_event("failed")
                    return True
                row.status = "processing"
                row.attempts += 1
                attempt = row.attempts
                row.lease_expires_at = now + self.lease_duration
                row.next_attempt_at = None
                row.updated_at = now
                job = {
                    "session_id": row.session_id,
                    "tenant_id": row.tenant_id,
                    "provider": row.provider,
                    "channel": row.channel,
                    "direction": row.direction,
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "ended_at": row.ended_at.isoformat(),
                    "encrypted_payload": row.encrypted_payload,
                }

        failure_stage = "payload_decryption"
        try:
            payload = json.loads(self._fernet.decrypt(job["encrypted_payload"].encode("ascii")))
            completed_call = CompletedCall(
                session_id=job["session_id"],
                tenant_id=job["tenant_id"],
                api_key_id=payload.get("api_key_id"),
                provider=job["provider"],
                channel=job["channel"],
                direction=job["direction"],
                started_at=job["started_at"],
                ended_at=job["ended_at"],
                transcript=payload["transcript"],
                recording_uri=payload.get("recording_uri"),
                post_call_context=payload.get("post_call_context"),
                output_schema=payload.get("output_schema"),
                tools=tuple(payload.get("tools", ())),
                time_zone=payload.get("time_zone", "UTC"),
                time_format=payload.get("time_format", "%Y-%m-%dT%H:%M:%S%z"),
                live_agent_context=payload.get("live_agent_context"),
            )
            failure_stage = "agent_processing"
            result = await asyncio.wait_for(
                agent.process(completed_call), timeout=self.lease_duration.total_seconds() / 2
            )
            failure_stage = "result_encryption"
            encrypted_result = self._fernet.encrypt(
                json.dumps(asdict(result), ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
        except Exception as exc:
            trace = traceback.extract_tb(exc.__traceback__)
            origin = trace[-1] if trace else None
            retry_at = datetime.now(UTC) + timedelta(seconds=min(2**attempt, 300))
            new_status = "queued" if attempt < self.max_attempts else "failed"
            async with self._sessions.begin() as session:
                await session.execute(
                    update(PostCallJobRow)
                    .where(
                        PostCallJobRow.session_id == job["session_id"],
                        PostCallJobRow.status == "processing",
                        PostCallJobRow.attempts == attempt,
                    )
                    .values(
                        status=new_status,
                        next_attempt_at=retry_at if new_status == "queued" else None,
                        lease_expires_at=None,
                        last_error_type=type(exc).__name__,
                        last_dispatched_at=None,
                        updated_at=datetime.now(UTC),
                    )
                )
            logger.error(
                "post_call_job_failed",
                extra={
                    "session_id": job["session_id"],
                    "tenant_id": job["tenant_id"],
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    # Exception messages can include call data. Stage and traceback
                    # location are enough to identify the failing code path safely.
                    "failure_stage": failure_stage,
                    "error_origin_file": origin.filename.rsplit("/", 1)[-1] if origin else None,
                    "error_origin_function": origin.name if origin else None,
                    "error_origin_line": origin.lineno if origin else None,
                },
            )
            post_call_job_event("retrying" if new_status == "queued" else "failed")
            return True

        delivery_enqueued = False
        async with self._sessions.begin() as session:
            finalized = await session.execute(
                update(PostCallJobRow)
                .where(
                    PostCallJobRow.session_id == job["session_id"],
                    PostCallJobRow.status == "processing",
                    PostCallJobRow.attempts == attempt,
                )
                .values(
                    status="completed",
                    encrypted_result=encrypted_result,
                    lease_expires_at=None,
                    last_error_type=None,
                    updated_at=datetime.now(UTC),
                )
            )
            webhook = None
            if finalized.rowcount == 1:
                webhook = await session.scalar(
                    select(PostCallWebhookConfigRow)
                    .where(PostCallWebhookConfigRow.tenant_id == job["tenant_id"])
                    .with_for_update()
                )
            if webhook is not None:
                session.add(
                    PostCallWebhookDeliveryRow(
                        event_id=f"post_call.analysis.completed:{job['session_id']}",
                        session_id=job["session_id"],
                        tenant_id=job["tenant_id"],
                        target_url=webhook.target_url,
                        encrypted_secret=webhook.encrypted_secret,
                        status="queued",
                        attempts=0,
                        next_attempt_at=datetime.now(UTC),
                        lease_expires_at=None,
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
                delivery_enqueued = True
        if delivery_enqueued:
            post_call_webhook_event("queued")
        logger.info(
            "post_call_job_completed",
            extra={"session_id": job["session_id"], "tenant_id": job["tenant_id"]},
        )
        post_call_job_event("completed")
        return True

    async def process_job(self, session_id: str, agent: PostCallAgent) -> bool:
        """Claim and process a specific Lambda event; duplicate/stale events safely no-op."""
        return await self.process_one(agent, session_id=session_id)

    async def process_delivery_one(self) -> bool:
        now = datetime.now(UTC)
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(PostCallWebhookDeliveryRow)
                    .where(
                        or_(
                            (PostCallWebhookDeliveryRow.status == "queued")
                            & (PostCallWebhookDeliveryRow.next_attempt_at <= now),
                            (PostCallWebhookDeliveryRow.status == "processing")
                            & (PostCallWebhookDeliveryRow.lease_expires_at <= now),
                        )
                    )
                    .order_by(PostCallWebhookDeliveryRow.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    return False
                if row.attempts >= self.max_attempts:
                    row.status = "failed"
                    row.target_url = ""
                    row.encrypted_secret = ""
                    row.next_attempt_at = None
                    row.lease_expires_at = None
                    row.last_error_type = "delivery_attempts_exhausted"
                    row.updated_at = now
                    delivery = None
                else:
                    row.status = "processing"
                    row.attempts += 1
                    row.next_attempt_at = None
                    row.lease_expires_at = now + self.lease_duration
                    row.updated_at = now
                    delivery = {
                        "event_id": row.event_id,
                        "session_id": row.session_id,
                        "tenant_id": row.tenant_id,
                        "target_url": row.target_url,
                        "encrypted_secret": row.encrypted_secret,
                        "attempts": row.attempts,
                    }
                    job = await session.get(PostCallJobRow, row.session_id)
                    if job is None or job.status != "completed" or not job.encrypted_result:
                        delivery["invalid_job"] = True
                    else:
                        delivery.update(
                            provider=job.provider,
                            channel=job.channel,
                            direction=job.direction,
                            started_at=job.started_at.isoformat() if job.started_at else None,
                            ended_at=job.ended_at.isoformat(),
                            encrypted_result=job.encrypted_result,
                            encrypted_payload=job.encrypted_payload,
                        )

        if delivery is None:
            logger.error("post_call_webhook_delivery_failed", extra={"error_type": "attempts_exhausted"})
            return True

        status_code = None
        failure_stage = "webhook_target_resolution"
        try:
            if delivery.get("invalid_job"):
                raise RuntimeError("completed_call_result_missing")
            parsed_target, pinned_address = await _resolve_public_webhook_target(delivery["target_url"])
            failure_stage = "webhook_payload_decryption"
            result = json.loads(
                self._fernet.decrypt(delivery["encrypted_result"].encode("ascii"))
            )
            call_payload = json.loads(
                self._fernet.decrypt(delivery["encrypted_payload"].encode("ascii"))
            )
            failure_stage = "webhook_payload_serialization"
            body = {
                "event": "call.analysis.completed",
                "event_id": delivery["event_id"],
                "call": {
                    "session_id": delivery["session_id"],
                    "provider": delivery["provider"],
                    "channel": delivery["channel"],
                    "direction": delivery["direction"],
                    "started_at": delivery["started_at"],
                    "ended_at": delivery["ended_at"],
                },
                "post_call": {
                    "time_zone": call_payload.get("time_zone", "UTC"),
                    "time_format": call_payload.get("time_format", "%Y-%m-%dT%H:%M:%S%z"),
                },
                "result": result,
            }
            body_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
            if len(body_bytes) > MAX_WEBHOOK_BODY_BYTES:
                raise RuntimeError("webhook_body_too_large")
            timestamp = str(int(time.time()))
            secret = self._fernet.decrypt(delivery["encrypted_secret"].encode("ascii"))
            signed = timestamp.encode("ascii") + b"." + delivery["event_id"].encode("utf-8") + b"." + body_bytes
            signature = hmac.new(secret, signed, hashlib.sha256).hexdigest()
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Chiefvoice-Webhooks/1.0",
                "X-Chiefvoice-Event": body["event"],
                "X-Chiefvoice-Event-Id": delivery["event_id"],
                "X-Chiefvoice-Timestamp": timestamp,
                "X-Chiefvoice-Signature": f"sha256={signature}",
            }
            ip_literal = f"[{pinned_address}]" if ":" in pinned_address else pinned_address
            pinned_url = urllib.parse.urlunsplit(
                (parsed_target.scheme, ip_literal, parsed_target.path or "/", parsed_target.query, "")
            )
            failure_stage = "webhook_http_delivery"
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=3.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                request = client.build_request(
                    "POST",
                    pinned_url,
                    content=body_bytes,
                    headers={**headers, "Host": parsed_target.hostname},
                    # httpcore passes this hostname to AnyIO's TLS connector,
                    # whose IDNA resolver expects str and encodes it itself.
                    # Supplying bytes raises AttributeError in idna2008_resolve.
                    extensions={"sni_hostname": parsed_target.hostname},
                )
                response = await client.send(request, stream=True)
                status_code = response.status_code
                await response.aclose()
            if not 200 <= status_code < 300:
                raise RuntimeError(f"webhook_http_{status_code}")
        except Exception as exc:
            error_type = type(exc).__name__
            if isinstance(exc, RuntimeError) and str(exc).startswith("webhook_http_"):
                error_type = str(exc)
            trace = traceback.extract_tb(exc.__traceback__)
            origin = trace[-1] if trace else None
            retry_at = datetime.now(UTC) + timedelta(
                seconds=min(2 ** delivery["attempts"], 3600)
            )
            new_status = "queued" if delivery["attempts"] < self.max_attempts else "failed"
            async with self._sessions.begin() as session:
                await session.execute(
                    update(PostCallWebhookDeliveryRow)
                    .where(
                        PostCallWebhookDeliveryRow.event_id == delivery["event_id"],
                        PostCallWebhookDeliveryRow.status == "processing",
                        PostCallWebhookDeliveryRow.attempts == delivery["attempts"],
                    )
                    .values(
                        status=new_status,
                        next_attempt_at=retry_at if new_status == "queued" else None,
                        lease_expires_at=None,
                        last_status_code=status_code,
                        last_error_type=error_type,
                        updated_at=datetime.now(UTC),
                        **(
                            {"target_url": "", "encrypted_secret": ""}
                            if new_status == "failed"
                            else {}
                        ),
                    )
                )
            logger.error(
                "post_call_webhook_delivery_failed",
                extra={
                    "session_id": delivery["session_id"],
                    "tenant_id": delivery["tenant_id"],
                    "attempt": delivery["attempts"],
                    "error_type": error_type,
                    "failure_stage": failure_stage,
                    # Keep callback URLs and exception text out of logs; source
                    # location is sufficient to diagnose a local failure.
                    "error_origin_file": origin.filename.rsplit("/", 1)[-1] if origin else None,
                    "error_origin_function": origin.name if origin else None,
                    "error_origin_line": origin.lineno if origin else None,
                },
            )
            post_call_webhook_event("retrying" if new_status == "queued" else "failed")
            return True

        async with self._sessions.begin() as session:
            await session.execute(
                update(PostCallWebhookDeliveryRow)
                .where(
                    PostCallWebhookDeliveryRow.event_id == delivery["event_id"],
                    PostCallWebhookDeliveryRow.status == "processing",
                    PostCallWebhookDeliveryRow.attempts == delivery["attempts"],
                )
                .values(
                    status="delivered",
                    target_url="",
                    encrypted_secret="",
                    lease_expires_at=None,
                    last_status_code=status_code,
                    last_error_type=None,
                    delivered_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        logger.info(
            "post_call_webhook_delivered",
            extra={
                "session_id": delivery["session_id"],
                "tenant_id": delivery["tenant_id"],
            },
        )
        post_call_webhook_event("delivered")
        return True


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


async def validate_public_webhook_url(target_url: str) -> None:
    """Reject SSRF destinations using the same resolution policy as delivery."""
    await _resolve_public_webhook_target(target_url)


async def _resolve_public_webhook_target(target_url: str) -> tuple[urllib.parse.SplitResult, str]:
    return await asyncio.to_thread(_resolve_public_webhook_target_sync, target_url)


def _resolve_public_webhook_target_sync(
    target_url: str,
) -> tuple[urllib.parse.SplitResult, str]:
    try:
        parsed = urllib.parse.urlsplit(target_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Webhook URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and port != 443)
    ):
        raise ValueError("Webhook URL must be HTTPS on port 443 without credentials or fragment")
    hostname = hostname.rstrip(".").lower()
    if (
        hostname in {"localhost", "metadata.google.internal"}
        or hostname.endswith((".local", ".internal"))
        or "." not in hostname
    ):
        raise ValueError("Webhook URL host is not public")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("Webhook URL must use a public DNS hostname, not an IP address")
    try:
        records = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Webhook URL hostname could not be resolved") from exc
    addresses = {record[4][0].split("%", 1)[0] for record in records}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Webhook URL must resolve only to public IP addresses")
    # Pin one checked address in the HTTPS URL and provide the original hostname as
    # SNI/Host, preserving certificate hostname validation while avoiding DNS TOCTOU.
    return parsed, sorted(addresses)[0]
