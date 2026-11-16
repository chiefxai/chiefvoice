from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from cryptography.fernet import Fernet
from sqlalchemy import DateTime, ForeignKey, Index, String, Text, delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from chiefvoice.accounts.models import TenantAccount
from chiefvoice.accounts.store import Base, TenantAccountRow
from chiefvoice.billing import BillingMode
from chiefvoice.core.db_schema import verify_schema_revision

API_KEY_PREFIX = "cv_live_"
_KEY_PATTERN = re.compile(r"^cv_live_([a-f0-9]{24})\.([A-Za-z0-9_-]{43})$")


@dataclass(frozen=True, slots=True)
class ApiKeyIdentity:
    key_id: str
    tenant_id: str
    subject: str
    scopes: frozenset[str]
    assigned_numbers: tuple[tuple[str, str], ...] = ()


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
    assigned_numbers: tuple[tuple[str, str], ...] = ()
    vertex_project_id: str | None = None
    vertex_location: str | None = None
    voice_location: str | None = None
    post_call_location: str | None = None
    vertex_credentials_configured: bool = False
    voice_provider: str = "vertex"
    voice_model: str = "google/gemini-live-2.5-flash-native-audio"
    post_call_provider: str = "vertex"
    post_call_model: str = "gemini-3.5-flash-lite"
    voice_agent_enabled: bool = True
    post_call_agent_enabled: bool = True
    voice_agent_id: str | None = None
    post_call_agent_id: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApiKeyVertexConfig:
    project_id: str | None
    location: str
    credentials_json: str | None
    voice_provider: str = "vertex"
    voice_model: str = "google/gemini-live-2.5-flash-native-audio"
    post_call_provider: str = "vertex"
    post_call_model: str = "gemini-3.5-flash-lite"
    voice_agent_enabled: bool = True
    post_call_agent_enabled: bool = True
    post_call_project_id: str | None = None
    post_call_location: str | None = None
    post_call_credentials_json: str | None = None
    auto_hangup_when_complete: bool = False


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
        assigned_numbers: tuple[tuple[str, str], ...] = (),
        vertex_project_id: str | None = None,
        vertex_location: str | None = None,
        vertex_credentials_json: str | None = None,
        voice_location: str | None = None,
        post_call_location: str | None = None,
        voice_provider: str = "vertex",
        voice_model: str = "google/gemini-live-2.5-flash-native-audio",
        post_call_provider: str = "vertex",
        post_call_model: str = "gemini-3.5-flash-lite",
        voice_agent_enabled: bool = True,
        post_call_agent_enabled: bool = True,
        voice_agent_id: str | None = None,
        post_call_agent_id: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> tuple[str, ApiKeySummary]: ...
    async def list_for_tenant(self, tenant_id: str) -> list[ApiKeySummary]: ...
    async def list_all(self) -> list[ApiKeySummary]: ...
    async def revoke(self, tenant_id: str, key_id: str) -> bool: ...
    async def delete_permanently(self, tenant_id: str, key_id: str) -> bool: ...
    async def replace_phone_numbers(
        self,
        tenant_id: str,
        key_id: str,
        assigned_numbers: tuple[tuple[str, str], ...],
    ) -> bool: ...
    async def get_vertex_config(self, key_id: str) -> ApiKeyVertexConfig | None: ...
    async def update_vertex_config(
        self, tenant_id: str, key_id: str, *, vertex_project_id: str,
        vertex_location: str, vertex_credentials_json: str | None,
    ) -> bool: ...
    async def update_config_and_numbers(
        self, tenant_id: str, key_id: str, *, assigned_numbers: tuple[tuple[str, str], ...],
        name: str,
        vertex_project_id: str, vertex_location: str, vertex_credentials_json: str | None,
        voice_provider: str = "vertex", voice_model: str = "google/gemini-live-2.5-flash-native-audio",
        post_call_provider: str = "vertex", post_call_model: str = "gemini-3.5-flash-lite",
        voice_agent_enabled: bool = True, post_call_agent_enabled: bool = True,
        voice_location: str | None = None, post_call_location: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> bool: ...
    async def provision_customer(
        self,
        *,
        display_name: str,
        billing_mode: BillingMode,
        created_by: str,
        key_name: str,
        scopes: tuple[str, ...],
        expires_at: datetime | None,
        assigned_numbers: tuple[tuple[str, str], ...] = (),
        vertex_project_id: str | None = None,
        vertex_location: str | None = None,
        vertex_credentials_json: str | None = None,
        voice_location: str | None = None,
        post_call_location: str | None = None,
        voice_provider: str = "vertex",
        voice_model: str = "google/gemini-live-2.5-flash-native-audio",
        post_call_provider: str = "vertex",
        post_call_model: str = "gemini-3.5-flash-lite",
        voice_agent_enabled: bool = True,
        post_call_agent_enabled: bool = True,
        voice_agent_id: str | None = None,
        post_call_agent_id: str | None = None,
        voice_plan: dict | None = None,
        tags: tuple[str, ...] = (),
    ) -> tuple[TenantAccount, str, ApiKeySummary]: ...


class ApiKeyRow(Base):
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_tenant_id", "tenant_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scopes_json: Mapped[str] = mapped_column(Text, nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    vertex_project_id: Mapped[str | None] = mapped_column(String(128))
    vertex_location: Mapped[str | None] = mapped_column(String(64))
    voice_location: Mapped[str | None] = mapped_column(String(64))
    post_call_location: Mapped[str | None] = mapped_column(String(64))
    encrypted_vertex_credentials: Mapped[str | None] = mapped_column(Text)
    voice_provider: Mapped[str] = mapped_column(String(40), nullable=False, default="vertex", server_default="vertex")
    voice_model: Mapped[str] = mapped_column(String(160), nullable=False, default="google/gemini-live-2.5-flash-native-audio", server_default="google/gemini-live-2.5-flash-native-audio")
    post_call_provider: Mapped[str] = mapped_column(String(40), nullable=False, default="vertex", server_default="vertex")
    post_call_model: Mapped[str] = mapped_column(String(160), nullable=False, default="gemini-3.5-flash-lite", server_default="gemini-3.5-flash-lite")
    voice_agent_enabled: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")
    post_call_agent_enabled: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")
    voice_agent_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("agent_provider_configs.id", ondelete="SET NULL"))
    post_call_agent_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("agent_provider_configs.id", ondelete="SET NULL"))


class ApiKeyPhoneNumberRow(Base):
    __tablename__ = "api_key_phone_numbers"
    __table_args__ = (
        Index("uq_api_key_phone_numbers_phone_number", "phone_number", unique=True),
        Index("ix_api_key_phone_numbers_api_key_id", "api_key_id"),
    )

    api_key_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(40), primary_key=True, nullable=False)
    phone_number: Mapped[str] = mapped_column(String(64), primary_key=True, nullable=False)


class SqlApiKeyDirectory:
    """Hash-only API key storage and verification backed by the shared Postgres schema."""

    def __init__(self, database_url: str, encryption_key: str | None = None):
        self._engine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._fernet = Fernet(encryption_key.encode("ascii")) if encryption_key else None

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
            bindings = await session.scalars(
                select(ApiKeyPhoneNumberRow)
                .where(ApiKeyPhoneNumberRow.api_key_id == row.id)
                .order_by(ApiKeyPhoneNumberRow.provider, ApiKeyPhoneNumberRow.phone_number)
            )
            return ApiKeyIdentity(
                key_id=row.id,
                tenant_id=row.tenant_id,
                subject=f"api_key:{row.id}",
                scopes=frozenset(json.loads(row.scopes_json)),
                assigned_numbers=tuple((item.provider, item.phone_number) for item in bindings),
            )

    async def create(
        self,
        *,
        tenant_id: str,
        name: str,
        scopes: tuple[str, ...],
        created_by: str,
        expires_at: datetime | None,
        assigned_numbers: tuple[tuple[str, str], ...] = (),
        vertex_project_id: str | None = None,
        vertex_location: str | None = None,
        vertex_credentials_json: str | None = None,
        voice_location: str | None = None,
        post_call_location: str | None = None,
        voice_provider: str = "vertex",
        voice_model: str = "google/gemini-live-2.5-flash-native-audio",
        post_call_provider: str = "vertex",
        post_call_model: str = "gemini-3.5-flash-lite",
        voice_agent_enabled: bool = True,
        post_call_agent_enabled: bool = True,
        voice_agent_id: str | None = None,
        post_call_agent_id: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> tuple[str, ApiKeySummary]:
        normalized_numbers = tuple(sorted(set(assigned_numbers)))
        if len(normalized_numbers) != len(assigned_numbers) or len(
            {number for _, number in assigned_numbers}
        ) != len(assigned_numbers):
            raise ValueError("API key phone number assignments must be unique")
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
            tags_json=json.dumps(tags),
            created_by=created_by,
            created_at=now,
            expires_at=expires_at,
            vertex_project_id=vertex_project_id,
            vertex_location=vertex_location,
            voice_location=voice_location,
            post_call_location=post_call_location,
            encrypted_vertex_credentials=self._encrypt_credentials(vertex_credentials_json),
            voice_provider=voice_provider, voice_model=voice_model,
            post_call_provider=post_call_provider, post_call_model=post_call_model,
            voice_agent_enabled=voice_agent_enabled, post_call_agent_enabled=post_call_agent_enabled,
            voice_agent_id=voice_agent_id, post_call_agent_id=post_call_agent_id,
        )
        async with self._sessions.begin() as session:
            account = await session.get(TenantAccountRow, tenant_id)
            if account is None:
                raise LookupError("Customer account does not exist")
            session.add(row)
            await session.flush()
            session.add_all(
                ApiKeyPhoneNumberRow(
                    api_key_id=key_id, provider=provider, phone_number=phone_number
                )
                for provider, phone_number in normalized_numbers
            )
            await session.flush()
        return token, _to_summary(row, normalized_numbers)

    def _encrypt_credentials(self, value: str | None) -> str | None:
        if value is None:
            return None
        if self._fernet is None:
            raise RuntimeError("CREDENTIALS_ENCRYPTION_KEY is required to store GCP credentials")
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    async def get_vertex_config(self, key_id: str) -> ApiKeyVertexConfig | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(ApiKeyRow).where(ApiKeyRow.id == key_id)
            )
            if row is None:
                return None
            from chiefvoice.agents.configuration_store import AgentConfigurationRow

            voice_agent = await session.get(AgentConfigurationRow, row.voice_agent_id) if row.voice_agent_id else None
            post_call_agent = await session.get(AgentConfigurationRow, row.post_call_agent_id) if row.post_call_agent_id else None
            credentials = None
            if row.encrypted_vertex_credentials:
                if self._fernet is None:
                    raise RuntimeError("CREDENTIALS_ENCRYPTION_KEY is not configured")
                credentials = self._fernet.decrypt(row.encrypted_vertex_credentials.encode("ascii"))
            return ApiKeyVertexConfig(
                voice_agent_enabled=row.voice_agent_enabled,
                post_call_agent_enabled=row.post_call_agent_enabled,
                post_call_project_id=row.vertex_project_id,
                post_call_location=row.post_call_location or (post_call_agent.location if post_call_agent else None) or row.vertex_location or "us-central1",
                post_call_credentials_json=credentials.decode("utf-8") if credentials else None,
                auto_hangup_when_complete=bool(voice_agent and voice_agent.auto_hangup_when_complete),
                project_id=row.vertex_project_id,
                location=row.voice_location or (voice_agent.location if voice_agent else None) or row.vertex_location or "us-central1",
                credentials_json=credentials.decode("utf-8") if credentials else None,
                voice_provider=voice_agent.provider if voice_agent else row.voice_provider,
                voice_model=voice_agent.model if voice_agent else row.voice_model,
                post_call_provider=post_call_agent.provider if post_call_agent else row.post_call_provider,
                post_call_model=post_call_agent.model if post_call_agent else row.post_call_model,
            )

    async def update_vertex_config(
        self, tenant_id: str, key_id: str, *, vertex_project_id: str,
        vertex_location: str, vertex_credentials_json: str | None,
        voice_provider: str = "vertex", voice_model: str = "google/gemini-live-2.5-flash-native-audio",
        post_call_provider: str = "vertex", post_call_model: str = "gemini-3.5-flash-lite",
        voice_agent_enabled: bool = True, post_call_agent_enabled: bool = True,
        voice_agent_id: str | None = None, post_call_agent_id: str | None = None,
        voice_location: str | None = None, post_call_location: str | None = None,
    ) -> bool:
        values = {"vertex_project_id": vertex_project_id, "vertex_location": vertex_location,
                  "voice_location": voice_location, "post_call_location": post_call_location,
                  "voice_provider": voice_provider, "voice_model": voice_model,
                  "post_call_provider": post_call_provider, "post_call_model": post_call_model}
        values.update(voice_agent_enabled=voice_agent_enabled, post_call_agent_enabled=post_call_agent_enabled)
        values.update(voice_agent_id=voice_agent_id, post_call_agent_id=post_call_agent_id)
        if vertex_credentials_json is not None:
            values["encrypted_vertex_credentials"] = self._encrypt_credentials(vertex_credentials_json)
        async with self._sessions.begin() as session:
            result = await session.execute(
                update(ApiKeyRow).where(
                    ApiKeyRow.id == key_id, ApiKeyRow.tenant_id == tenant_id,
                    ApiKeyRow.revoked_at.is_(None),
                ).values(**values)
            )
            return result.rowcount == 1

    async def update_config_and_numbers(
        self, tenant_id: str, key_id: str, *, assigned_numbers: tuple[tuple[str, str], ...],
        name: str,
        vertex_project_id: str, vertex_location: str, vertex_credentials_json: str | None,
        voice_provider: str = "vertex", voice_model: str = "google/gemini-live-2.5-flash-native-audio",
        post_call_provider: str = "vertex", post_call_model: str = "gemini-3.5-flash-lite",
        voice_agent_enabled: bool = True, post_call_agent_enabled: bool = True,
        voice_agent_id: str | None = None, post_call_agent_id: str | None = None,
        voice_location: str | None = None, post_call_location: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> bool:
        normalized = tuple(sorted(set(assigned_numbers)))
        if len(normalized) != len(assigned_numbers) or len({number for _, number in assigned_numbers}) != len(assigned_numbers):
            raise ValueError("API key phone number assignments must be unique")
        values = {"vertex_project_id": vertex_project_id, "vertex_location": vertex_location,
                  "voice_location": voice_location, "post_call_location": post_call_location,
                  "voice_provider": voice_provider, "voice_model": voice_model,
                  "post_call_provider": post_call_provider, "post_call_model": post_call_model}
        values.update(voice_agent_enabled=voice_agent_enabled, post_call_agent_enabled=post_call_agent_enabled)
        values.update(voice_agent_id=voice_agent_id, post_call_agent_id=post_call_agent_id)
        values["name"] = name
        values["tags_json"] = json.dumps(tags)
        if vertex_credentials_json is not None:
            values["encrypted_vertex_credentials"] = self._encrypt_credentials(vertex_credentials_json)
        async with self._sessions.begin() as session:
            row = await session.scalar(select(ApiKeyRow).where(
                ApiKeyRow.id == key_id, ApiKeyRow.tenant_id == tenant_id,
                ApiKeyRow.revoked_at.is_(None),
            ).with_for_update())
            if row is None:
                return False
            await session.execute(delete(ApiKeyPhoneNumberRow).where(ApiKeyPhoneNumberRow.api_key_id == key_id))
            session.add_all(ApiKeyPhoneNumberRow(api_key_id=key_id, provider=provider, phone_number=number) for provider, number in normalized)
            await session.execute(update(ApiKeyRow).where(ApiKeyRow.id == key_id).values(**values))
            await session.flush()
            return True

    async def provision_customer(
        self,
        *,
        display_name: str,
        billing_mode: BillingMode,
        created_by: str,
        key_name: str,
        scopes: tuple[str, ...],
        expires_at: datetime | None,
        assigned_numbers: tuple[tuple[str, str], ...] = (),
        vertex_project_id: str | None = None,
        vertex_location: str | None = None,
        vertex_credentials_json: str | None = None,
        voice_location: str | None = None,
        post_call_location: str | None = None,
        voice_provider: str = "vertex",
        voice_model: str = "google/gemini-live-2.5-flash-native-audio",
        post_call_provider: str = "vertex",
        post_call_model: str = "gemini-3.5-flash-lite",
        voice_agent_enabled: bool = True,
        post_call_agent_enabled: bool = True,
        voice_agent_id: str | None = None,
        post_call_agent_id: str | None = None,
        voice_plan: dict | None = None,
        tags: tuple[str, ...] = (),
    ) -> tuple[TenantAccount, str, ApiKeySummary]:
        """Atomically create a stable customer tenant and its first API credential."""
        from uuid import uuid4

        tenant_id = str(uuid4())
        key_id = secrets.token_hex(12)
        token = f"{API_KEY_PREFIX}{key_id}.{secrets.token_urlsafe(32)}"
        now = datetime.now(UTC)
        account_row = TenantAccountRow(
            tenant_id=tenant_id,
            display_name=display_name,
            billing_mode=billing_mode.value,
            created_by=created_by,
            created_at=now,
        )
        key_row = ApiKeyRow(
            id=key_id,
            tenant_id=tenant_id,
            name=key_name,
            secret_hash=_digest(token),
            scopes_json=json.dumps(sorted(set(scopes)), separators=(",", ":")),
            tags_json=json.dumps(tags),
            created_by=created_by,
            created_at=now,
            expires_at=expires_at,
            vertex_project_id=vertex_project_id,
            vertex_location=vertex_location,
            voice_location=voice_location,
            post_call_location=post_call_location,
            encrypted_vertex_credentials=self._encrypt_credentials(vertex_credentials_json),
            voice_provider=voice_provider,
            voice_model=voice_model,
            post_call_provider=post_call_provider,
            post_call_model=post_call_model,
            voice_agent_enabled=voice_agent_enabled,
            post_call_agent_enabled=post_call_agent_enabled,
            voice_agent_id=voice_agent_id,
            post_call_agent_id=post_call_agent_id,
        )
        normalized_numbers = tuple(sorted(set(assigned_numbers)))
        if len(normalized_numbers) != len(assigned_numbers) or len({number for _, number in assigned_numbers}) != len(assigned_numbers):
            raise ValueError("API key phone number assignments must be unique")
        async with self._sessions.begin() as session:
            session.add(account_row)
            session.add(key_row)
            await session.flush()
            session.add_all(
                ApiKeyPhoneNumberRow(api_key_id=key_id, provider=provider, phone_number=number)
                for provider, number in normalized_numbers
            )
            if voice_plan is not None:
                await session.execute(
                    text("""
                        INSERT INTO tenant_voice_plans
                            (tenant_id, billing_mode, voice_enabled, currency,
                             rate_per_minute_minor, max_call_seconds, allowed_channels,
                             max_concurrent_sessions, recording_allowed,
                             prepaid_balance_minor, postpaid_credit_limit_minor,
                             postpaid_used_minor, created_at, updated_at)
                        VALUES
                            (:tenant_id, :billing_mode, :voice_enabled, :currency,
                             :rate_per_minute_minor, :max_call_seconds,
                             CAST(:allowed_channels AS JSON), :max_concurrent_sessions,
                             :recording_allowed, :prepaid_balance_minor,
                             :postpaid_credit_limit_minor, 0, :created_at, :updated_at)
                    """),
                    {
                        **voice_plan,
                        "tenant_id": tenant_id,
                        "billing_mode": billing_mode.value,
                        "allowed_channels": json.dumps(voice_plan["allowed_channels"]),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            await session.flush()
        account = TenantAccount(
            tenant_id=tenant_id,
            display_name=display_name,
            billing_mode=billing_mode,
            created_by=created_by,
            created_at=now,
        )
        return account, token, _to_summary(key_row, normalized_numbers)

    async def list_for_tenant(self, tenant_id: str) -> list[ApiKeySummary]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(ApiKeyRow)
                .where(ApiKeyRow.tenant_id == tenant_id)
                .order_by(ApiKeyRow.created_at.desc())
            )
            return await _summaries_for_rows(session, list(rows))

    async def list_all(self) -> list[ApiKeySummary]:
        async with self._sessions() as session:
            rows = await session.scalars(select(ApiKeyRow).order_by(ApiKeyRow.created_at.desc()))
            return await _summaries_for_rows(session, list(rows))

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
            if result.rowcount != 1:
                return False
            await session.execute(
                delete(ApiKeyPhoneNumberRow).where(ApiKeyPhoneNumberRow.api_key_id == key_id)
            )
            return True

    async def delete_permanently(self, tenant_id: str, key_id: str) -> bool:
        """Permanently remove an API key and its number bindings from Postgres."""
        async with self._sessions.begin() as session:
            result = await session.execute(
                delete(ApiKeyRow).where(
                    ApiKeyRow.id == key_id,
                    ApiKeyRow.tenant_id == tenant_id,
                )
            )
            return result.rowcount == 1

    async def replace_phone_numbers(
        self,
        tenant_id: str,
        key_id: str,
        assigned_numbers: tuple[tuple[str, str], ...],
    ) -> bool:
        normalized = tuple(sorted(set(assigned_numbers)))
        if len(normalized) != len(assigned_numbers) or len(
            {number for _, number in assigned_numbers}
        ) != len(assigned_numbers):
            raise ValueError("API key phone number assignments must be unique")
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(ApiKeyRow)
                .where(
                    ApiKeyRow.id == key_id,
                    ApiKeyRow.tenant_id == tenant_id,
                    ApiKeyRow.revoked_at.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                return False
            await session.execute(
                delete(ApiKeyPhoneNumberRow).where(ApiKeyPhoneNumberRow.api_key_id == key_id)
            )
            session.add_all(
                ApiKeyPhoneNumberRow(
                    api_key_id=key_id, provider=provider, phone_number=phone_number
                )
                for provider, phone_number in normalized
            )
            await session.flush()
            return True


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def _summaries_for_rows(session, rows: list[ApiKeyRow]) -> list[ApiKeySummary]:
    if not rows:
        return []
    bindings = await session.scalars(
        select(ApiKeyPhoneNumberRow).where(
            ApiKeyPhoneNumberRow.api_key_id.in_([row.id for row in rows])
        )
    )
    by_key: dict[str, list[tuple[str, str]]] = {}
    for item in bindings:
        by_key.setdefault(item.api_key_id, []).append((item.provider, item.phone_number))
    return [_to_summary(row, tuple(sorted(by_key.get(row.id, [])))) for row in rows]


def _to_summary(
    row: ApiKeyRow, assigned_numbers: tuple[tuple[str, str], ...] = ()
) -> ApiKeySummary:
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
        assigned_numbers=assigned_numbers,
        vertex_project_id=row.vertex_project_id,
        vertex_location=row.vertex_location,
        voice_location=row.voice_location,
        post_call_location=row.post_call_location,
        vertex_credentials_configured=bool(row.encrypted_vertex_credentials),
        voice_provider=row.voice_provider, voice_model=row.voice_model,
        post_call_provider=row.post_call_provider, post_call_model=row.post_call_model,
        voice_agent_enabled=row.voice_agent_enabled,
        post_call_agent_enabled=row.post_call_agent_enabled,
        voice_agent_id=row.voice_agent_id,
        post_call_agent_id=row.post_call_agent_id,
        tags=tuple(json.loads(row.tags_json or "[]")),
    )
