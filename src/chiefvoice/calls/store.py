"""Durable, tenant-scoped call lifecycle and idempotency storage."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from chiefvoice.accounts.store import Base
from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.agents.post_call.queue import PostCallJobRow
from chiefvoice.core.db_schema import verify_schema_revision
from chiefvoice.data_lifecycle.retention import RetentionClass
from chiefvoice.sessions import AcceptedSession, Direction

logger = logging.getLogger(__name__)


class CallSessionRow(Base):
    __tablename__ = "call_sessions"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    api_key_id: Mapped[str | None] = mapped_column(String(32), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_call_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    encrypted_session: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_characters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transcript_persistence_failed: Mapped[bool] = mapped_column(nullable=False, default=False)
    queue_enqueued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    queue_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    queue_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    post_call_enqueued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboundCallQueueRow(Base):
    __tablename__ = "outbound_call_queue"
    __table_args__ = (
        Index("ix_outbound_call_queue_status_enqueued", "status", "enqueued_at"),
        Index("ix_outbound_call_queue_status_lease", "status", "lease_expires_at"),
    )

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("call_sessions.session_id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdempotencyConflict(ValueError):
    """An idempotency key was reused with a different request body."""


class SqlCallSessionStore:
    """Encrypted call envelope storage with durable idempotency and transcript checkpoints."""

    data_class = RetentionClass.TRANSCRIPT

    def __init__(self, database_url: str, encryption_key: str):
        self._engine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._fernet = Fernet(encryption_key.encode("ascii"))

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    @staticmethod
    def fingerprint(request_data: dict[str, Any]) -> str:
        encoded = json.dumps(
            request_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def register_outbound(
        self,
        session: AcceptedSession,
        idempotency_key: str | None,
        fingerprint: str,
    ) -> tuple[AcceptedSession, bool, str]:
        """Return (session, newly-created, current-status), atomically deduping tenant keys."""
        now = datetime.now(UTC)
        encrypted = self._encrypt_session(session)
        async with self._sessions.begin() as db:
            stmt = insert(CallSessionRow).values(
                session_id=session.session_id,
                tenant_id=session.tenant_id,
                api_key_id=session.api_key_id,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                status="starting",
                provider_call_id=session.provider_call_id,
                encrypted_session=encrypted,
                encrypted_transcript=None,
                transcript_characters=0,
                transcript_persistence_failed=False,
                started_at=session.started_at,
                answered_at=None,
                ended_at=None,
                end_reason=None,
                post_call_enqueued_at=None,
                created_at=now,
                updated_at=now,
            )
            if idempotency_key is not None:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=[CallSessionRow.tenant_id, CallSessionRow.idempotency_key]
                )
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=[CallSessionRow.session_id])
            inserted = await db.execute(stmt)
            if inserted.rowcount == 1:
                return session, True, "starting"
            row = await db.scalar(
                select(CallSessionRow)
                .where(
                    CallSessionRow.tenant_id == session.tenant_id,
                    CallSessionRow.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
            if row is None:
                # A random UUID collision is extraordinarily unlikely; surface it instead
                # of silently pretending that the request was accepted.
                raise RuntimeError("Could not persist the outbound call session")
            if row.request_fingerprint != fingerprint:
                raise IdempotencyConflict(
                    "Idempotency key was already used for a different call request"
                )
            if row.encrypted_session is None:
                raise IdempotencyConflict(
                    "This idempotency key refers to a retained call; use a new key"
                )
            return self._decrypt_session(row.encrypted_session), False, row.status

    async def enqueue_outbound(
        self, session: AcceptedSession, expires_at: datetime
    ) -> bool:
        """Atomically move a registered outbound session into the durable FIFO queue."""
        if not session.tenant_id:
            raise ValueError("A tenant-owned outbound session is required")
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("Queue expiration must be timezone-aware")
        now = datetime.now(UTC)
        expires_at = expires_at.astimezone(UTC)
        if expires_at <= now:
            raise ValueError("Queue expiration must be in the future")
        async with self._sessions.begin() as db:
            row = await db.scalar(
                select(CallSessionRow)
                .where(
                    CallSessionRow.tenant_id == session.tenant_id,
                    CallSessionRow.session_id == session.session_id,
                )
                .with_for_update()
            )
            if row is None or row.encrypted_session is None:
                raise LookupError("Outbound call session is not registered")
            if row.status == "queued":
                existing = await db.get(OutboundCallQueueRow, session.session_id)
                return existing is not None
            if row.status != "starting":
                return False

            session.metadata["call_status"] = "queued"
            row.status = "queued"
            row.queue_enqueued_at = now
            row.queue_expires_at = expires_at
            row.queue_attempts = 0
            row.end_reason = None
            row.updated_at = now
            row.encrypted_session = self._encrypt_session(session)
            db.add(
                OutboundCallQueueRow(
                    session_id=session.session_id,
                    tenant_id=session.tenant_id,
                    enqueued_at=now,
                    expires_at=expires_at,
                    status="queued",
                    lease_token=None,
                    lease_expires_at=None,
                    available_at=now,
                    attempts=0,
                    updated_at=now,
                )
            )
            return True

    async def claim_outbound(self, lease_seconds: int = 120) -> AcceptedSession | None:
        """Claim the oldest ready call, recovering expired leases without redialing starts."""
        if not 1 <= lease_seconds <= 3_600:
            raise ValueError("Outbound queue lease must be between 1 and 3600 seconds")
        now = datetime.now(UTC)
        claimed: AcceptedSession | None = None
        async with self._sessions.begin() as db:
            recovery_rows = await db.execute(
                select(OutboundCallQueueRow, CallSessionRow)
                .join(
                    CallSessionRow,
                    CallSessionRow.session_id == OutboundCallQueueRow.session_id,
                )
                .where(
                    or_(
                        (
                            (OutboundCallQueueRow.status == "dispatching")
                            & (OutboundCallQueueRow.lease_expires_at <= now)
                        ),
                        (
                            (OutboundCallQueueRow.status == "queued")
                            & (OutboundCallQueueRow.expires_at <= now)
                        ),
                    )
                )
                .order_by(OutboundCallQueueRow.enqueued_at)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            for queue_row, call_row in recovery_rows:
                if queue_row.status == "dispatching":
                    if call_row.status == "starting":
                        call_row.status = "reconciliation_required"
                        call_row.end_reason = "provider_start_outcome_ambiguous"
                        call_row.updated_at = now
                        await db.delete(queue_row)
                    elif call_row.status == "queued":
                        if queue_row.expires_at <= now:
                            call_row.status = "expired"
                            call_row.end_reason = "outbound_queue_expired"
                            call_row.ended_at = now
                            call_row.updated_at = now
                            await db.delete(queue_row)
                        else:
                            queue_row.status = "queued"
                            queue_row.lease_token = None
                            queue_row.lease_expires_at = None
                            queue_row.available_at = now
                            queue_row.updated_at = now
                    else:
                        await db.delete(queue_row)
                elif queue_row.status == "queued" and call_row.status == "queued":
                    call_row.status = "expired"
                    call_row.end_reason = "outbound_queue_expired"
                    call_row.ended_at = now
                    call_row.updated_at = now
                    await db.delete(queue_row)

            candidate = await db.execute(
                select(OutboundCallQueueRow, CallSessionRow)
                .join(
                    CallSessionRow,
                    CallSessionRow.session_id == OutboundCallQueueRow.session_id,
                )
                .where(
                    OutboundCallQueueRow.status == "queued",
                    OutboundCallQueueRow.available_at <= now,
                    OutboundCallQueueRow.expires_at > now,
                    CallSessionRow.status == "queued",
                )
                .order_by(OutboundCallQueueRow.enqueued_at, OutboundCallQueueRow.session_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            item = candidate.first()
            if item is not None:
                queue_row, call_row = item
                if not call_row.encrypted_session:
                    call_row.status = "failed"
                    call_row.end_reason = "queued_session_unavailable"
                    call_row.updated_at = now
                    await db.delete(queue_row)
                else:
                    claimed = self._decrypt_session(call_row.encrypted_session)
                    lease_token = secrets.token_hex(24)
                    claimed.metadata["call_status"] = "queued"
                    claimed.metadata["queue_status"] = "dispatching"
                    # The lease token is runtime-only and intentionally omitted by
                    # _encrypt_session, so it is never stored in encrypted_session.
                    claimed.metadata["outbound_queue_lease_token"] = lease_token
                    queue_row.status = "dispatching"
                    queue_row.attempts += 1
                    queue_row.lease_token = lease_token
                    queue_row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                    queue_row.updated_at = now
                    call_row.queue_attempts += 1
                    call_row.updated_at = now
        return claimed

    async def renew_outbound_lease(
        self, session_id: str, lease_token: str, lease_seconds: int
    ) -> bool:
        """Extend a live outbound dispatch lease owned by the matching worker token."""
        if not 1 <= lease_seconds <= 3_600:
            raise ValueError("Outbound queue lease must be between 1 and 3600 seconds")
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            result = await db.execute(
                update(OutboundCallQueueRow)
                .where(
                    OutboundCallQueueRow.session_id == session_id,
                    OutboundCallQueueRow.status == "dispatching",
                    OutboundCallQueueRow.lease_token == lease_token,
                    OutboundCallQueueRow.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    updated_at=now,
                )
            )
            return result.rowcount == 1

    async def defer_outbound(
        self,
        session_id: str,
        retry_at: datetime,
        lease_token: str,
    ) -> bool:
        """Release a claimed slot for a known pre-dial deferral, preserving FIFO age."""
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            raise ValueError("Outbound queue retry time must be timezone-aware")
        retry_at = retry_at.astimezone(UTC)
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            queue_row = await db.scalar(
                select(OutboundCallQueueRow)
                .where(OutboundCallQueueRow.session_id == session_id)
                .with_for_update()
            )
            call_row = await db.scalar(
                select(CallSessionRow)
                .where(CallSessionRow.session_id == session_id)
                .with_for_update()
            )
            if (
                queue_row is None
                or call_row is None
                or queue_row.status != "dispatching"
                or call_row.status not in {"queued", "starting"}
                or queue_row.lease_token != lease_token
            ):
                return False
            if queue_row.expires_at <= now or retry_at >= queue_row.expires_at:
                call_row.status = "expired"
                call_row.end_reason = "outbound_queue_expired"
                call_row.ended_at = now
                call_row.updated_at = now
                await db.delete(queue_row)
                return True
            if not call_row.encrypted_session:
                call_row.status = "failed"
                call_row.end_reason = "queued_session_unavailable"
                call_row.updated_at = now
                await db.delete(queue_row)
                return False

            deferred_session = self._decrypt_session(call_row.encrypted_session)
            deferred_session.metadata["call_status"] = "queued"
            deferred_session.metadata.pop("outbound_queue_lease_token", None)
            call_row.encrypted_session = self._encrypt_session(deferred_session)
            call_row.status = "queued"
            call_row.end_reason = None
            call_row.updated_at = now
            queue_row.status = "queued"
            queue_row.lease_token = None
            queue_row.lease_expires_at = None
            queue_row.available_at = retry_at
            queue_row.updated_at = now
            return True

    async def remove_outbound(
        self, session_id: str, lease_token: str
    ) -> bool:
        """Remove a dispatched queue row only after the session reached a safe terminal start state."""
        async with self._sessions.begin() as db:
            queue_row = await db.scalar(
                select(OutboundCallQueueRow)
                .where(OutboundCallQueueRow.session_id == session_id)
                .with_for_update()
            )
            call_row = await db.scalar(
                select(CallSessionRow)
                .where(CallSessionRow.session_id == session_id)
                .with_for_update()
            )
            if queue_row is None or call_row is None:
                return False
            if queue_row.status != "dispatching":
                return False
            if lease_token is None or queue_row.lease_token != lease_token:
                return False
            if call_row.status in {"queued", "starting"}:
                return False
            await db.delete(queue_row)
            return True

    async def cancel_outbound(self, tenant_id: str, session_id: str) -> bool:
        """Cancel only a tenant-owned outbound call that has not been claimed."""
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            call_row = await db.scalar(
                select(CallSessionRow)
                .where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.session_id == session_id,
                )
                .with_for_update()
            )
            if call_row is None or call_row.status != "queued":
                return False
            queue_row = await db.scalar(
                select(OutboundCallQueueRow)
                .where(
                    OutboundCallQueueRow.tenant_id == tenant_id,
                    OutboundCallQueueRow.session_id == session_id,
                    OutboundCallQueueRow.status == "queued",
                )
                .with_for_update()
            )
            if queue_row is None:
                return False
            call_row.status = "cancelled"
            call_row.end_reason = "cancelled_before_dispatch"
            call_row.ended_at = now
            call_row.updated_at = now
            await db.delete(queue_row)
            return True

    async def get_outbound_queue_status(
        self, tenant_id: str, session_id: str
    ) -> dict[str, Any] | None:
        """Return queue details for a tenant-owned call without decrypting its payload."""
        async with self._sessions() as db:
            call_row = await db.scalar(
                select(CallSessionRow).where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.session_id == session_id,
                )
            )
            if call_row is None:
                return None
            queue_row = await db.get(OutboundCallQueueRow, session_id)
            queue_status = (
                queue_row.status
                if queue_row is not None
                else call_row.status
                if call_row.status in {"expired", "cancelled"}
                else None
            )
            return {
                "queue_status": queue_status,
                "queued_at": (
                    call_row.queue_enqueued_at.isoformat()
                    if call_row.queue_enqueued_at
                    else None
                ),
                "queue_expires_at": (
                    call_row.queue_expires_at.isoformat()
                    if call_row.queue_expires_at
                    else None
                ),
                "queue_attempts": call_row.queue_attempts,
                "queue_lease_expires_at": (
                    queue_row.lease_expires_at.isoformat()
                    if queue_row and queue_row.lease_expires_at
                    else None
                ),
            }

    async def register_inbound(self, session: AcceptedSession) -> tuple[AcceptedSession, bool, str]:
        """Persist an inbound session once, keyed by its deterministic session ID.

        Vobiz can retry an inbound webhook. The adapter derives session_id from the
        provider and call ID, so the primary key makes those retries idempotent.
        """
        if session.direction is not Direction.INBOUND or not session.tenant_id:
            raise ValueError("An inbound call session with a tenant is required")
        now = datetime.now(UTC)
        fingerprint = self.fingerprint(
            {
                "provider": session.provider,
                "channel": session.channel,
                "tenant_id": session.tenant_id,
                "provider_call_id": session.provider_call_id,
                "caller": session.caller,
                "callee": session.callee,
                "bot_config_id": session.bot_config_id,
            }
        )
        async with self._sessions.begin() as db:
            stmt = (
                insert(CallSessionRow)
                .values(
                    session_id=session.session_id,
                    tenant_id=session.tenant_id,
                    api_key_id=session.api_key_id,
                    idempotency_key=None,
                    request_fingerprint=fingerprint,
                    status="starting",
                    provider_call_id=session.provider_call_id,
                    encrypted_session=self._encrypt_session(session),
                    encrypted_transcript=None,
                    transcript_characters=0,
                    transcript_persistence_failed=False,
                    started_at=session.started_at,
                    answered_at=None,
                    ended_at=None,
                    end_reason=None,
                    post_call_enqueued_at=None,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=[CallSessionRow.session_id])
            )
            inserted = await db.execute(stmt)
            if inserted.rowcount == 1:
                return session, True, "starting"
            row = await db.scalar(
                select(CallSessionRow)
                .where(CallSessionRow.session_id == session.session_id)
                .with_for_update()
            )
            if row is None:
                raise RuntimeError("Could not persist the inbound call session")
            if row.tenant_id != session.tenant_id or row.request_fingerprint != fingerprint:
                raise IdempotencyConflict(
                    "Provider call ID is already associated with a different inbound call"
                )
            if row.encrypted_session is None:
                raise IdempotencyConflict(
                    "This inbound call has expired from retained call details"
                )
            return self._decrypt_session(row.encrypted_session), False, row.status

    async def update_state(
        self,
        session: AcceptedSession,
        *,
        status: str,
        provider_call_id: str | None = None,
        answered_at: datetime | None = None,
        ended_at: datetime | None = None,
        end_reason: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        session_blob = self._encrypt_session(session)
        values: dict[str, Any] = {
            "status": status,
            "api_key_id": session.api_key_id,
            "encrypted_session": session_blob,
            "transcript_persistence_failed": bool(
                session.metadata.get("transcript_persistence_error")
            ),
            "updated_at": now,
        }
        if provider_call_id:
            values["provider_call_id"] = provider_call_id
        if answered_at:
            values["answered_at"] = answered_at
        if ended_at:
            values["ended_at"] = ended_at
        if end_reason:
            values["end_reason"] = end_reason
        async with self._sessions.begin() as db:
            await db.execute(
                update(CallSessionRow)
                .where(
                    CallSessionRow.tenant_id == session.tenant_id,
                    CallSessionRow.session_id == session.session_id,
                    CallSessionRow.status != "completed",
                )
                .values(**values)
            )

    async def get_session(self, tenant_id: str, session_id: str) -> AcceptedSession | None:
        """Load the encrypted call envelope for a provider callback reference."""
        async with self._sessions() as db:
            row = await db.scalar(
                select(CallSessionRow).where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.session_id == session_id,
                )
            )
            if row is None or row.encrypted_session is None:
                return None
            session = self._decrypt_session(row.encrypted_session)
            # The answer timestamp is stored alongside the encrypted session so
            # provider hangup callbacks can distinguish unanswered outbound calls
            # even when their signed state was created before the answer event.
            session.metadata["call_answered"] = row.answered_at is not None
            return session

    async def get_post_call_workflow_config(
        self, tenant_id: str, session_id: str, template_directory: Any | None = None
    ) -> AcceptedSession | None:
        """Load authoritative workflow identity from the encrypted call config.

        Older queue payloads may omit workflow_id. A template can supply it only
        when the persisted session pins the same template revision and API-key
        assignment; prompt text and call context are never inspected.
        """
        stored = await self.get_session(tenant_id, session_id)
        if stored is None or stored.post_call_workflow_id or template_directory is None:
            return stored
        template_id = stored.post_call_template_id
        revision = stored.post_call_template_revision
        if not template_id or revision is None:
            return stored
        if stored.api_key_id:
            template = await template_directory.get_for_api(stored.api_key_id, template_id)
        else:
            template = await template_directory.get(template_id, include_disabled=True)
        if (
            template is None
            or template.kind != "post_call"
            or template.revision != revision
            or template.api_key_id != stored.api_key_id
        ):
            return stored
        workflow_id = template.settings.get("workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id:
            return stored
        return replace(stored, post_call_workflow_id=workflow_id)

    async def append_transcript(
        self, session_id: str, tenant_id: str, speaker: str, text: str
    ) -> None:
        if not text or speaker not in {"Caller", "Assistant"}:
            return
        async with self._sessions.begin() as db:
            row = await db.scalar(
                select(CallSessionRow)
                .where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.session_id == session_id,
                )
                .with_for_update()
            )
            if row is None or row.status == "completed" or row.encrypted_session is None:
                return
            current = ""
            if row.encrypted_transcript:
                current = self._fernet.decrypt(row.encrypted_transcript.encode("ascii")).decode(
                    "utf-8"
                )
            segment = f"{speaker}: {text.strip()}"
            if not segment:
                return
            joined = f"{current}\n{segment}" if current else segment
            if len(joined) > 1_000_000:
                joined = joined[:1_000_000]
            row.encrypted_transcript = self._fernet.encrypt(joined.encode("utf-8")).decode("ascii")
            row.transcript_characters = len(joined)
            row.updated_at = datetime.now(UTC)

    async def mark_post_call_enqueued(self, tenant_id: str, session_id: str) -> None:
        async with self._sessions.begin() as db:
            await db.execute(
                update(CallSessionRow)
                .where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.session_id == session_id,
                    CallSessionRow.post_call_enqueued_at.is_(None),
                )
                .values(post_call_enqueued_at=datetime.now(UTC), updated_at=datetime.now(UTC))
            )

    async def flag_stale_outbound_starts(self, older_than: timedelta, limit: int = 100) -> int:
        """Mark uncertain provider submissions for operator reconciliation, never retry them."""
        if older_than.total_seconds() <= 0 or limit < 1:
            raise ValueError("A positive age and batch limit are required")
        cutoff = datetime.now(UTC) - older_than
        async with self._sessions.begin() as db:
            rows = await db.scalars(
                select(CallSessionRow)
                .where(
                    CallSessionRow.status == "starting",
                    CallSessionRow.updated_at <= cutoff,
                )
                .order_by(CallSessionRow.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            stale = list(rows)
            for row in stale:
                row.status = "reconciliation_required"
                row.end_reason = "provider_start_outcome_ambiguous"
                row.updated_at = datetime.now(UTC)
            return len(stale)

    async def resolve_outbound_start(
        self,
        tenant_id: str,
        session_id: str,
        *,
        confirmed_started: bool,
        provider_call_id: str | None = None,
    ) -> str:
        """Record an operator-confirmed provider outcome without ever redialing."""
        if confirmed_started and (not provider_call_id or len(provider_call_id) > 256):
            raise ValueError("A valid provider call ID is required for a started call")
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            row = await db.scalar(
                select(CallSessionRow)
                .where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.session_id == session_id,
                )
                .with_for_update()
            )
            if row is None:
                raise LookupError("Call session not found")
            if (
                not confirmed_started
                and row.status == "failed"
                and row.end_reason == "provider_confirmed_not_started"
            ):
                # Lets an operator retry the idempotent billing release after an
                # infrastructure interruption without changing the call decision.
                return "confirmed_not_started"
            if row.status != "reconciliation_required":
                raise ValueError("Call is not awaiting provider-start reconciliation")
            if confirmed_started:
                session = self._decrypt_session(row.encrypted_session or "")
                session.provider_call_id = provider_call_id
                row.provider_call_id = provider_call_id
                row.encrypted_session = self._encrypt_session(session)
                row.status = "initiated"
                row.end_reason = "provider_confirmed_started"
                row.updated_at = now
                return "confirmed_started"
            row.status = "failed"
            row.end_reason = "provider_confirmed_not_started"
            row.ended_at = now
            row.encrypted_session = None
            row.encrypted_transcript = None
            row.transcript_characters = 0
            row.updated_at = now
            return "confirmed_not_started"

    async def pending_post_call(self, limit: int = 100) -> list[CompletedCall]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(CallSessionRow)
                .outerjoin(
                    PostCallJobRow,
                    PostCallJobRow.session_id == CallSessionRow.session_id,
                )
                .where(
                    CallSessionRow.status == "completed",
                    or_(
                        CallSessionRow.post_call_enqueued_at.is_(None),
                        PostCallJobRow.session_id.is_(None),
                        PostCallJobRow.status.is_(None),
                        PostCallJobRow.status == "",
                    ),
                    CallSessionRow.encrypted_session.is_not(None),
                )
                .order_by(CallSessionRow.ended_at)
                .limit(limit)
            )
            calls = []
            for row in rows:
                session = self._decrypt_session(row.encrypted_session or "")
                if not session.tenant_id:
                    logger.error(
                        "post_call_reconciliation_skipped",
                        extra={
                            "session_id": session.session_id,
                            "reason": "tenant_id_missing",
                        },
                    )
                    continue
                if not session.post_call_agent_enabled:
                    await self.mark_post_call_enqueued(session.tenant_id, session.session_id)
                    logger.info(
                        "post_call_reconciliation_skipped",
                        extra={
                            "session_id": session.session_id,
                            "tenant_id": session.tenant_id,
                            "reason": "agent_disabled",
                        },
                    )
                    continue
                transcript = (
                    self._fernet.decrypt(row.encrypted_transcript.encode("ascii")).decode("utf-8")
                    if row.encrypted_transcript
                    else ""
                )
                recording_uri = session.metadata.get("recording_uri")
                has_recording = isinstance(recording_uri, str) and bool(recording_uri.strip())
                if (
                    not transcript.strip()
                    and not has_recording
                    and not session.post_call_workflow_id
                ):
                    # This is the crash-recovery path for calls that ended without
                    # either transcript text or a recording. There is no safe work
                    # item to enqueue, so terminalize the reconciliation marker to
                    # keep these rows from starving recoverable calls on every scan.
                    await self.mark_post_call_enqueued(session.tenant_id, session.session_id)
                    logger.warning(
                        "post_call_reconciliation_skipped",
                        extra={
                            "session_id": session.session_id,
                            "tenant_id": session.tenant_id,
                            "reason": "no_transcript_recording_or_workflow",
                        },
                    )
                    continue
                calls.append(
                    CompletedCall(
                        session_id=session.session_id,
                        tenant_id=session.tenant_id,
                        api_key_id=session.api_key_id,
                        provider=session.provider,
                        channel=session.channel,
                        direction=session.direction.value,
                        live_agent_language=session.live_agent_language,
                        started_at=session.started_at.isoformat(),
                        ended_at=(row.ended_at or row.updated_at).isoformat(),
                        transcript=transcript,
                        post_call_context=session.post_call_context,
                        output_schema=session.post_call_output_schema,
                        tools=tuple(session.post_call_tools or ()),
                        time_zone=session.post_call_time_zone or "UTC",
                        time_format=session.post_call_time_format or "%Y-%m-%dT%H:%M:%S%z",
                        live_agent_context=(
                            session.live_agent_context
                            if session.include_live_agent_context
                            else None
                        ),
                        workflow_id=session.post_call_workflow_id,
                        workflow_context=session.post_call_workflow_context,
                        end_call_claim=session.metadata.get("end_call_claim"),
                        tool_events=tuple(session.metadata.get("tool_events", ())),
                        failure_webhook_url=session.post_call_failure_webhook_url,
                        failure_webhook_payload=session.post_call_failure_webhook_payload,
                        recording_uri=recording_uri if has_recording else None,
                    )
                )
            return calls

    async def get_status(self, tenant_id: str, session_id: str) -> dict[str, Any] | None:
        async with self._sessions() as db:
            row = await db.scalar(
                select(CallSessionRow).where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.session_id == session_id,
                )
            )
            if row is None:
                return None
            stored_session = (
                self._decrypt_session(row.encrypted_session)
                if row.encrypted_session
                else None
            )
            queue_row = await db.get(OutboundCallQueueRow, session_id)
            queue_status = (
                queue_row.status
                if queue_row is not None
                else row.status
                if row.status in {"expired", "cancelled"}
                else None
            )
            return {
                "session_id": row.session_id,
                "provider": (
                    stored_session.provider if stored_session else None
                ),
                "status": row.status,
                "queue_status": queue_status,
                "queued_at": (
                    row.queue_enqueued_at.isoformat() if row.queue_enqueued_at else None
                ),
                "queue_expires_at": (
                    row.queue_expires_at.isoformat() if row.queue_expires_at else None
                ),
                "queue_attempts": row.queue_attempts,
                "queue_lease_expires_at": (
                    queue_row.lease_expires_at.isoformat()
                    if queue_row and queue_row.lease_expires_at
                    else None
                ),
                "provider_call_id": row.provider_call_id,
                "started_at": row.started_at.isoformat(),
                "answered_at": row.answered_at.isoformat() if row.answered_at else None,
                "ended_at": row.ended_at.isoformat() if row.ended_at else None,
                "end_reason": row.end_reason,
                "transcript_available": bool(row.encrypted_transcript),
                "transcript_persistence_failed": row.transcript_persistence_failed,
                "post_call_enqueued": row.post_call_enqueued_at is not None,
                "post_call_agent_enabled": (
                    stored_session.post_call_agent_enabled if stored_session else None
                ),
                "recording_available": bool(
                    stored_session and stored_session.metadata.get("recording_uri")
                ),
            }

    async def get_transcript(self, tenant_id: str, session_id: str) -> dict[str, Any] | None:
        """Load a tenant-owned transcript and its availability state."""
        async with self._sessions() as db:
            row = await db.scalar(
                select(CallSessionRow).where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.session_id == session_id,
                )
            )
            if row is None:
                return None
            transcript = None
            if row.encrypted_transcript:
                transcript = self._fernet.decrypt(
                    row.encrypted_transcript.encode("ascii")
                ).decode("utf-8")
            return {
                "session_id": row.session_id,
                "status": row.status,
                "transcript_available": bool(transcript),
                "transcript_characters": row.transcript_characters,
                "transcript_persistence_failed": row.transcript_persistence_failed,
                "transcript": transcript,
            }

    async def list_for_api_key(
        self, tenant_id: str, api_key_id: str, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        async with self._sessions() as db:
            count = await db.scalar(
                select(func.count()).select_from(CallSessionRow).where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.api_key_id == api_key_id,
                )
            )
            rows = await db.scalars(
                select(CallSessionRow)
                .where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.api_key_id == api_key_id,
                )
                .order_by(CallSessionRow.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            items = []
            for row in rows:
                session = self._decrypt_session(row.encrypted_session) if row.encrypted_session else None
                duration = None
                if row.answered_at and row.ended_at:
                    duration = max(0, int((row.ended_at - row.answered_at).total_seconds()))
                items.append({
                    "session_id": row.session_id,
                    "provider": session.provider if session else None,
                    "channel": session.channel if session else None,
                    "direction": session.direction.value if session else None,
                    "caller": session.caller if session else None,
                    "callee": session.callee if session else None,
                    "status": row.status,
                    "end_reason": row.end_reason,
                    "started_at": row.started_at.isoformat(),
                    "answered_at": row.answered_at.isoformat() if row.answered_at else None,
                    "ended_at": row.ended_at.isoformat() if row.ended_at else None,
                    "duration_seconds": duration,
                    "transcript_available": bool(row.encrypted_transcript),
                    "recording_available": bool(session and session.metadata.get("recording_uri")),
                    "post_call_agent_enabled": session.post_call_agent_enabled if session else True,
                })
            return {"items": items, "total": int(count or 0), "limit": limit, "offset": offset}

    async def get_api_call_detail(
        self, tenant_id: str, api_key_id: str, session_id: str
    ) -> dict[str, Any] | None:
        """Load one API-owned call, including its decrypted transcript, for the admin console."""
        async with self._sessions() as db:
            row = await db.scalar(
                select(CallSessionRow).where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.api_key_id == api_key_id,
                    CallSessionRow.session_id == session_id,
                )
            )
            if row is None:
                return None
            session = self._decrypt_session(row.encrypted_session) if row.encrypted_session else None
            transcript = None
            if row.encrypted_transcript:
                transcript = self._fernet.decrypt(
                    row.encrypted_transcript.encode("ascii")
                ).decode("utf-8")
            duration = None
            if row.answered_at and row.ended_at:
                duration = max(0, int((row.ended_at - row.answered_at).total_seconds()))
            return {
                "session_id": row.session_id,
                "provider": session.provider if session else None,
                "channel": session.channel if session else None,
                "direction": session.direction.value if session else None,
                "caller": session.caller if session else None,
                "callee": session.callee if session else None,
                "status": row.status,
                "end_reason": row.end_reason,
                "started_at": row.started_at.isoformat(),
                "answered_at": row.answered_at.isoformat() if row.answered_at else None,
                "ended_at": row.ended_at.isoformat() if row.ended_at else None,
                "duration_seconds": duration,
                "transcript": transcript,
                "transcript_available": bool(transcript),
                "recording_available": bool(session and session.metadata.get("recording_uri")),
                "recording_allowed": bool(session and session.recording_allowed),
                "post_call_agent_enabled": session.post_call_agent_enabled if session else True,
            }

    async def matches_phone_binding(
        self, tenant_id: str, session_id: str, provider: str, phone_number: str
    ) -> bool:
        async with self._sessions() as db:
            row = await db.scalar(
                select(CallSessionRow).where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.session_id == session_id,
                )
            )
            if row is None or not row.encrypted_session:
                return False
            session = self._decrypt_session(row.encrypted_session)
            number = session.caller if session.direction is Direction.OUTBOUND else session.callee
            return session.provider == provider and number == phone_number

    async def get_recording_uri(self, tenant_id: str, session_id: str) -> str | None:
        """Return a recording reference only for a call owned by this tenant."""
        async with self._sessions() as db:
            row = await db.scalar(
                select(CallSessionRow).where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.session_id == session_id,
                )
            )
            if row is None or not row.encrypted_session:
                return None
            session = self._decrypt_session(row.encrypted_session)
            recording_uri = session.metadata.get("recording_uri")
            return recording_uri if isinstance(recording_uri, str) else None

    async def purge_expired(self, tenant_id: str, cutoff: datetime, batch_size: int) -> int:
        """Purge transcript/context after terminal processing; keep idempotency tombstones."""
        if not tenant_id or cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("Tenant and timezone-aware cutoff are required")
        if batch_size < 1:
            raise ValueError("Purge batch size must be positive")
        async with self._sessions.begin() as db:
            rows = await db.scalars(
                select(CallSessionRow)
                .where(
                    CallSessionRow.tenant_id == tenant_id,
                    CallSessionRow.ended_at.is_not(None),
                    CallSessionRow.ended_at < cutoff,
                    or_(
                        and_(
                            CallSessionRow.status.in_(("completed", "failed")),
                            CallSessionRow.post_call_enqueued_at.is_not(None),
                            CallSessionRow.encrypted_transcript.is_not(None),
                        ),
                        and_(
                            CallSessionRow.status.in_(("expired", "cancelled")),
                            CallSessionRow.encrypted_session.is_not(None),
                        ),
                    ),
                )
                .order_by(CallSessionRow.ended_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            expired = list(rows)
            for row in expired:
                row.encrypted_transcript = None
                row.encrypted_session = None
                row.transcript_characters = 0
                row.updated_at = datetime.now(UTC)
            return len(expired)

    def _encrypt_session(self, session: AcceptedSession) -> str:
        payload = {
            "session_id": session.session_id,
            "channel": session.channel,
            "provider": session.provider,
            "direction": session.direction.value,
            "caller": session.caller,
            "callee": session.callee,
            "provider_call_id": session.provider_call_id,
            "tenant_id": session.tenant_id,
            "api_key_id": session.api_key_id,
            "bot_config_id": session.bot_config_id,
            "live_agent_instructions": session.live_agent_instructions,
            "live_agent_template_id": session.live_agent_template_id,
            "live_agent_template_revision": session.live_agent_template_revision,
            "live_agent_context": session.live_agent_context,
            "live_agent_language": session.live_agent_language,
            "live_agent_completion_requirements": session.live_agent_completion_requirements,
            "post_call_context": session.post_call_context,
            "post_call_template_id": session.post_call_template_id,
            "post_call_template_revision": session.post_call_template_revision,
            "post_call_failure_webhook_url": session.post_call_failure_webhook_url,
            "post_call_failure_webhook_payload": session.post_call_failure_webhook_payload,
            "post_call_output_schema": session.post_call_output_schema,
            "post_call_tools": session.post_call_tools,
            "post_call_time_zone": session.post_call_time_zone,
            "post_call_time_format": session.post_call_time_format,
            "post_call_workflow_id": session.post_call_workflow_id,
            "post_call_workflow_context": session.post_call_workflow_context,
            "include_live_agent_context": session.include_live_agent_context,
            "post_call_agent_enabled": session.post_call_agent_enabled,
            "auto_hangup_when_complete": session.auto_hangup_when_complete,
            "max_call_seconds": session.max_call_seconds,
            "recording_allowed": session.recording_allowed,
            "recording_uri": session.metadata.get("recording_uri"),
            "end_call_claim": session.metadata.get("end_call_claim"),
            "tool_events": session.metadata.get("tool_events", []),
            "started_at": session.started_at.isoformat(),
        }
        encrypted = self._fernet.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return encrypted.decode("ascii")

    def _decrypt_session(self, encrypted: str) -> AcceptedSession:
        payload = json.loads(self._fernet.decrypt(encrypted.encode("ascii")).decode("utf-8"))
        session = AcceptedSession(
            session_id=payload["session_id"],
            channel=payload["channel"],
            provider=payload["provider"],
            direction=Direction(payload["direction"]),
            caller=payload.get("caller"),
            callee=payload.get("callee"),
            provider_call_id=payload.get("provider_call_id"),
            tenant_id=payload.get("tenant_id"),
            api_key_id=payload.get("api_key_id"),
            bot_config_id=payload.get("bot_config_id"),
            live_agent_instructions=payload.get("live_agent_instructions"),
            live_agent_template_id=payload.get("live_agent_template_id"),
            live_agent_template_revision=payload.get("live_agent_template_revision"),
            live_agent_context=payload.get("live_agent_context"),
            live_agent_language=payload.get("live_agent_language"),
            live_agent_completion_requirements=payload.get("live_agent_completion_requirements"),
            post_call_context=payload.get("post_call_context"),
            post_call_template_id=payload.get("post_call_template_id"),
            post_call_template_revision=payload.get("post_call_template_revision"),
            post_call_failure_webhook_url=payload.get("post_call_failure_webhook_url"),
            post_call_failure_webhook_payload=payload.get("post_call_failure_webhook_payload"),
            post_call_output_schema=payload.get("post_call_output_schema"),
            post_call_tools=payload.get("post_call_tools"),
            post_call_time_zone=payload.get("post_call_time_zone"),
            post_call_time_format=payload.get("post_call_time_format"),
            post_call_workflow_id=payload.get("post_call_workflow_id"),
            post_call_workflow_context=payload.get("post_call_workflow_context"),
            include_live_agent_context=payload.get("include_live_agent_context", False),
            post_call_agent_enabled=payload.get("post_call_agent_enabled", True),
            auto_hangup_when_complete=payload.get("auto_hangup_when_complete", False),
            max_call_seconds=payload.get("max_call_seconds"),
            recording_allowed=payload.get("recording_allowed", False),
            started_at=datetime.fromisoformat(payload["started_at"]),
        )
        if payload.get("recording_uri"):
            session.metadata["recording_uri"] = payload["recording_uri"]
        claim = payload.get("end_call_claim", payload.get("live_call_claim"))
        if claim is not None:
            session.metadata["end_call_claim"] = claim
        events = payload.get("tool_events", payload.get("identity_tool_events"))
        if isinstance(events, list):
            session.metadata["tool_events"] = events
        return session
