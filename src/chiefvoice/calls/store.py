"""Durable, tenant-scoped call lifecycle and idempotency storage."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from chiefvoice.accounts.store import Base
from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.core.db_schema import verify_schema_revision
from chiefvoice.data_lifecycle.retention import RetentionClass
from chiefvoice.sessions import AcceptedSession, Direction


class CallSessionRow(Base):
    __tablename__ = "call_sessions"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)

    session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_call_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    encrypted_session: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_characters: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transcript_persistence_failed: Mapped[bool] = mapped_column(nullable=False, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    post_call_enqueued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
                .where(
                    CallSessionRow.status == "completed",
                    CallSessionRow.post_call_enqueued_at.is_(None),
                    CallSessionRow.encrypted_transcript.is_not(None),
                    CallSessionRow.transcript_persistence_failed.is_(False),
                    CallSessionRow.encrypted_session.is_not(None),
                )
                .order_by(CallSessionRow.ended_at)
                .limit(limit)
            )
            calls = []
            for row in rows:
                session = self._decrypt_session(row.encrypted_session or "")
                transcript = self._fernet.decrypt(row.encrypted_transcript.encode("ascii")).decode(
                    "utf-8"
                )
                if not session.tenant_id or not transcript.strip():
                    continue
                calls.append(
                    CompletedCall(
                        session_id=session.session_id,
                        tenant_id=session.tenant_id,
                        provider=session.provider,
                        channel=session.channel,
                        direction=session.direction.value,
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
                        recording_uri=session.metadata.get("recording_uri"),
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
            return {
                "session_id": row.session_id,
                "provider": (
                    self._decrypt_session(row.encrypted_session).provider
                    if row.encrypted_session
                    else None
                ),
                "status": row.status,
                "provider_call_id": row.provider_call_id,
                "started_at": row.started_at.isoformat(),
                "answered_at": row.answered_at.isoformat() if row.answered_at else None,
                "ended_at": row.ended_at.isoformat() if row.ended_at else None,
                "end_reason": row.end_reason,
                "transcript_available": bool(row.encrypted_transcript),
                "transcript_persistence_failed": row.transcript_persistence_failed,
                "post_call_enqueued": row.post_call_enqueued_at is not None,
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
                    CallSessionRow.status.in_(("completed", "failed")),
                    CallSessionRow.post_call_enqueued_at.is_not(None),
                    CallSessionRow.encrypted_transcript.is_not(None),
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
            "bot_config_id": session.bot_config_id,
            "live_agent_context": session.live_agent_context,
            "post_call_context": session.post_call_context,
            "post_call_output_schema": session.post_call_output_schema,
            "post_call_tools": session.post_call_tools,
            "post_call_time_zone": session.post_call_time_zone,
            "post_call_time_format": session.post_call_time_format,
            "include_live_agent_context": session.include_live_agent_context,
            "max_call_seconds": session.max_call_seconds,
            "recording_allowed": session.recording_allowed,
            "recording_uri": session.metadata.get("recording_uri"),
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
            bot_config_id=payload.get("bot_config_id"),
            live_agent_context=payload.get("live_agent_context"),
            post_call_context=payload.get("post_call_context"),
            post_call_output_schema=payload.get("post_call_output_schema"),
            post_call_tools=payload.get("post_call_tools"),
            post_call_time_zone=payload.get("post_call_time_zone"),
            post_call_time_format=payload.get("post_call_time_format"),
            include_live_agent_context=payload.get("include_live_agent_context", False),
            max_call_seconds=payload.get("max_call_seconds"),
            recording_allowed=payload.get("recording_allowed", False),
            started_at=datetime.fromisoformat(payload["started_at"]),
        )
        if payload.get("recording_uri"):
            session.metadata["recording_uri"] = payload["recording_uri"]
        return session
