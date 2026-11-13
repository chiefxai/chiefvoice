from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import DateTime, Index, String, Text, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from chiefvoice.accounts.store import Base
from chiefvoice.core.db_schema import verify_schema_revision

API_KEY_PREFIX = "cv_live_"
_KEY_PATTERN = re.compile(r"^cv_live_([a-f0-9]{24})\.([A-Za-z0-9_-]{43})$")


@dataclass(frozen=True, slots=True)
class ApiKeyIdentity:
    key_id: str
    tenant_id: str
    subject: str
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class ApiKeySummary:
    key_id: str
    tenant_id: str
    name: str
    key_prefix: str
    scopes: tuple[str, ...]
    created_by: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyDirectory(Protocol):
    async def authenticate(self, token: str) -> ApiKeyIdentity | None: ...
    async def create(
        self,
        *,
        tenant_id: str,
        name: str,
        scopes: tuple[str, ...],
        created_by: str,
        expires_at: datetime | None,
    ) -> tuple[str, ApiKeySummary]: ...
    async def list_for_tenant(self, tenant_id: str) -> list[ApiKeySummary]: ...
    async def revoke(self, tenant_id: str, key_id: str) -> bool: ...


class ApiKeyRow(Base):
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_tenant_id", "tenant_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scopes_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SqlApiKeyDirectory:
    """Hash-only API key storage and verification backed by the shared Postgres schema."""

    def __init__(self, database_url: str):
        self._engine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def initialize(self) -> None:
        await verify_schema_revision(self._engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def authenticate(self, token: str) -> ApiKeyIdentity | None:
        match = _KEY_PATTERN.fullmatch(token)
        if match is None:
            return None
        key_id = match.group(1)
        supplied_hash = _digest(token)
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            row = await session.scalar(select(ApiKeyRow).where(ApiKeyRow.id == key_id))
            if (
                row is None
                or row.revoked_at is not None
                or (row.expires_at is not None and row.expires_at <= now)
                or not hmac.compare_digest(row.secret_hash, supplied_hash)
            ):
                return None
            await session.execute(
                update(ApiKeyRow).where(ApiKeyRow.id == key_id).values(last_used_at=now)
            )
            return ApiKeyIdentity(
                key_id=row.id,
                tenant_id=row.tenant_id,
                subject=f"api_key:{row.id}",
                scopes=frozenset(json.loads(row.scopes_json)),
            )

    async def create(
        self,
        *,
        tenant_id: str,
        name: str,
        scopes: tuple[str, ...],
        created_by: str,
        expires_at: datetime | None,
    ) -> tuple[str, ApiKeySummary]:
        key_id = secrets.token_hex(12)
        secret = secrets.token_urlsafe(32)
        token = f"{API_KEY_PREFIX}{key_id}.{secret}"
        now = datetime.now(UTC)
        if expires_at is not None and (expires_at.tzinfo is None or expires_at.utcoffset() is None):
            raise ValueError("API key expiration must include a timezone")
        row = ApiKeyRow(
            id=key_id,
            tenant_id=tenant_id,
            name=name,
            secret_hash=_digest(token),
            scopes_json=json.dumps(sorted(set(scopes)), separators=(",", ":")),
            created_by=created_by,
            created_at=now,
            expires_at=expires_at,
        )
        async with self._sessions.begin() as session:
            session.add(row)
            await session.flush()
        return token, _to_summary(row)

    async def list_for_tenant(self, tenant_id: str) -> list[ApiKeySummary]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(ApiKeyRow)
                .where(ApiKeyRow.tenant_id == tenant_id)
                .order_by(ApiKeyRow.created_at.desc())
            )
            return [_to_summary(row) for row in rows]

    async def revoke(self, tenant_id: str, key_id: str) -> bool:
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(ApiKeyRow)
                .where(
                    ApiKeyRow.id == key_id,
                    ApiKeyRow.tenant_id == tenant_id,
                    ApiKeyRow.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC))
            )
            return result.rowcount == 1


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _to_summary(row: ApiKeyRow) -> ApiKeySummary:
    return ApiKeySummary(
        key_id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        key_prefix=f"{API_KEY_PREFIX}{row.id[:8]}...",
        scopes=tuple(sorted(json.loads(row.scopes_json))),
        created_by=row.created_by,
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )
