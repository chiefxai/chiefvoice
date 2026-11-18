"""Platform-admin customer provisioning and credential lifecycle APIs."""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Security, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError

from chiefvoice.accounts.models import AccountStore, PLATFORM_OWNER_ID, TenantAccount
from chiefvoice.agents.configurations import AgentConfiguration
from chiefvoice.agents.post_call.queue import validate_public_webhook_url
from chiefvoice.billing import BillingMode
from chiefvoice.channels.integration_store import SqlProviderIntegrationStore
from chiefvoice.channels.integrations import ProviderIntegration, ProviderPhoneNumber
from chiefvoice.calls.routes import (
    OutboundCallBody,
    _render_live,
    _render_post_call,
    _resolve_call_templates,
    get_call_coordinator,
    resolve_assigned_sender,
)
from chiefvoice.core.config import settings
from chiefvoice.core.tags import normalize_tags
from chiefvoice.identity.api_keys import ApiKeyDirectory, ApiKeySummary
from chiefvoice.identity.auth import Principal, current_principal
from chiefvoice.observability.setup import call_start_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/admin/customers", tags=["platform administration"])

CUSTOMER_API_KEY_SCOPES = frozenset(
    {
        "accounts:read",
        "agents:read",
        "agents:write",
        "billing:read",
        "calls:read",
        "calls:write",
        "integrations:read",
        "integrations:write",
        "webhooks:read",
        "webhooks:write",
    }
)
DEFAULT_CUSTOMER_API_KEY_SCOPES = [
    "calls:read",
    "calls:write",
    "webhooks:read",
    "webhooks:write",
]
MAX_API_KEY_LIFETIME = timedelta(days=365)


class CreateCustomerBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    billing_mode: BillingMode
    api_key_name: str = Field(default="Primary API key", min_length=1, max_length=100)
    api_key_scopes: list[str] = Field(
        default_factory=lambda: DEFAULT_CUSTOMER_API_KEY_SCOPES.copy(),
        min_length=1,
        max_length=10,
    )
    api_key_expires_at: datetime | None = None

    @field_validator("display_name", "api_key_name")
    @classmethod
    def trim_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be blank")
        return value

    @field_validator("api_key_scopes")
    @classmethod
    def validate_scopes(cls, scopes: list[str]) -> list[str]:
        normalized = [scope.strip() for scope in scopes]
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("At least one unique API key scope is required")
        if unknown := set(normalized) - CUSTOMER_API_KEY_SCOPES:
            raise ValueError(f"Unsupported customer API key scopes: {', '.join(sorted(unknown))}")
        return sorted(normalized)


class CreateCustomerApiKeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1, max_length=10)
    expires_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank")
        return value

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, scopes: list[str]) -> list[str]:
        normalized = [scope.strip() for scope in scopes]
        if not normalized or len(set(normalized)) != len(normalized):
            raise ValueError("At least one unique API key scope is required")
        if unknown := set(normalized) - CUSTOMER_API_KEY_SCOPES:
            raise ValueError(f"Unsupported customer API key scopes: {', '.join(sorted(unknown))}")
        return sorted(normalized)


class PhoneNumberAssignment(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    phone_number: str = Field(min_length=5, max_length=64)


class CreatePhoneApiKeyBody(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    billing_mode: BillingMode = BillingMode.PREPAID
    prepaid_balance_minor: int = Field(default=0, ge=0, le=10**15)
    postpaid_credit_limit_minor: int = Field(default=0, ge=0, le=10**15)
    tags: list[str] = Field(default_factory=list, max_length=20)
    phone_numbers: list[PhoneNumberAssignment] = Field(min_length=1, max_length=100)
    voice_agent_id: str | None = None
    post_call_agent_id: str | None = None
    gcp_credentials_json: str = Field(min_length=1, max_length=20000)
    voice_location: str | None = Field(default=None, max_length=64)
    post_call_location: str | None = Field(default=None, max_length=64)
    recording_allowed: bool = False
    post_call_webhook_url: str | None = Field(default=None, min_length=8, max_length=2048)

    @field_validator("customer_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be blank")
        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags_field(cls, value: list[str]) -> list[str]:
        return list(normalize_tags(value))

class UpdatePhoneApiKeyConfigBody(BaseModel):
    name: str = Field(default="Phone API", min_length=1, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=20)
    phone_numbers: list[PhoneNumberAssignment] = Field(min_length=1, max_length=100)
    voice_agent_id: str | None = None
    post_call_agent_id: str | None = None
    gcp_credentials_json: str | None = Field(default=None, max_length=20000)
    voice_location: str | None = Field(default=None, max_length=64)
    post_call_location: str | None = Field(default=None, max_length=64)
    recording_allowed: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("API name cannot be blank")
        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags_field(cls, value: list[str]) -> list[str]:
        return list(normalize_tags(value))


class AdjustApiBillingBody(BaseModel):
    prepaid_balance_minor: int | None = Field(default=None, ge=0, le=10**15)
    postpaid_credit_limit_minor: int | None = Field(default=None, ge=0, le=10**15)



def _parse_gcp_service_account(value: str) -> tuple[dict, str]:
    if not value.strip() or len(value) > 20000:
        raise HTTPException(status_code=422, detail="GCP service account JSON must be 1 to 20000 characters")
    try:
        credentials = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="GCP credentials must be valid service account JSON") from exc
    required = ("type", "project_id", "client_email", "private_key", "token_uri")
    if not isinstance(credentials, dict) or any(not credentials.get(key) for key in required):
        raise HTTPException(status_code=422, detail="Provide a complete Google Cloud service account JSON key")
    if credentials["type"] != "service_account":
        raise HTTPException(status_code=422, detail="GCP credentials must be a service account key")
    return credentials, json.dumps(credentials, separators=(",", ":"))


class ReplacePhoneApiKeyNumbersBody(BaseModel):
    phone_numbers: list[PhoneNumberAssignment] = Field(max_length=100)


class AddPlatformNumbersBody(BaseModel):
    credential_mode: Literal["platform", "custom"]
    phone_numbers: list[str] = Field(min_length=1, max_length=100)
    standard_channels: int = Field(default=1, ge=0, le=1_000)
    unlimited_channels: int = Field(default=0, ge=0, le=1_000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    auth_id: str | None = Field(default=None, max_length=200)
    auth_token: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_channel_capacity(self):
        if self.standard_channels + self.unlimited_channels < 1:
            raise ValueError("At least one standard or unlimited channel is required")
        return self

    @field_validator("tags")
    @classmethod
    def normalize_tags_field(cls, value: list[str]) -> list[str]:
        return list(normalize_tags(value))

    @field_validator("phone_numbers")
    @classmethod
    def normalize_phone_numbers(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            number = re.sub(r"[\s().-]", "", value)
            if not re.fullmatch(r"\+[1-9]\d{4,14}", number):
                raise ValueError("Phone numbers must be in international format, for example +15551234567")
            normalized.append(number)
        if len(normalized) != len(set(normalized)):
            raise ValueError("Phone numbers must be unique")
        return normalized


class UpdatePlatformNumberBody(BaseModel):
    inbound_enabled: bool
    outbound_enabled: bool
    standard_channels: int = Field(ge=0, le=1_000)
    unlimited_channels: int = Field(ge=0, le=1_000)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def require_channel_capacity(self):
        if self.standard_channels + self.unlimited_channels < 1:
            raise ValueError("At least one standard or unlimited channel is required")
        return self

    @field_validator("tags")
    @classmethod
    def normalize_tags_field(cls, value: list[str]) -> list[str]:
        return list(normalize_tags(value))


def _vobiz_credentials(credential_mode: str, auth_id: str | None, auth_token: str | None) -> dict[str, str]:
    if credential_mode == "platform":
        if not settings.vobiz_auth_id or not settings.vobiz_auth_token:
            raise HTTPException(status_code=503, detail="Platform Vobiz credentials are not configured")
        return {"auth_id": settings.vobiz_auth_id, "auth_token": settings.vobiz_auth_token}
    if not auth_id or not auth_id.strip() or not auth_token or not auth_token.strip():
        raise HTTPException(status_code=422, detail="Custom Vobiz auth ID and auth token are required")
    return {"auth_id": auth_id.strip(), "auth_token": auth_token.strip()}


def _platform_admin(required_scope: str):
    async def dependency(
        principal: Annotated[Principal, Security(current_principal)],
    ) -> Principal:
        if (
            principal.auth_method != "oidc"
            or principal.subject not in settings.platform_admin_subjects
        ):
            raise HTTPException(status_code=403, detail="Platform administrator access is required")
        if required_scope not in principal.scopes:
            raise HTTPException(status_code=403, detail="Insufficient platform administrator scope")
        return principal

    return dependency


def _stores(request: Request) -> tuple[AccountStore, ApiKeyDirectory]:
    account_store = getattr(request.app.state, "account_store", None)
    api_key_directory = getattr(request.app.state, "api_key_directory", None)
    if account_store is None or api_key_directory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Customer provisioning storage is not configured",
        )
    return account_store, api_key_directory


def _integration_store(request: Request) -> SqlProviderIntegrationStore:
    store = getattr(request.app.state, "provider_integration_store", None)
    if store is None or not hasattr(store, "list_phone_numbers"):
        raise HTTPException(status_code=503, detail="Phone number storage is not configured")
    return store


async def _sync_api_channel_capacity(request: Request, changed_number: tuple[str, str]) -> None:
    """Keep each API plan's concurrent call limit aligned with its assigned lines."""
    billing = getattr(request.app.state, "billing_directory", None)
    api_key_directory = getattr(request.app.state, "api_key_directory", None)
    if billing is None or api_key_directory is None or not hasattr(billing, "get_plan"):
        return
    records = await _integration_store(request).list_phone_numbers()
    capacities = {
        (item.provider, item.number): (
            item.standard_channels + item.unlimited_channels if item.outbound_enabled else 0
        )
        for item in records
    }
    for key in await api_key_directory.list_all():
        if changed_number not in key.assigned_numbers:
            continue
        plan = await billing.get_plan(key.tenant_id)
        if plan is None:
            continue
        capacity = min(
            1_000,
            sum(capacities.get(assigned, 0) for assigned in key.assigned_numbers),
        )
        try:
            await billing.configure(
                tenant_id=key.tenant_id,
                billing_mode=BillingMode(plan["billing_mode"]),
                voice_enabled=plan["voice_enabled"],
                currency=plan["currency"],
                rate_per_minute_minor=plan["rate_per_minute_minor"],
                max_call_seconds=plan["max_call_seconds"],
                allowed_channels=plan["allowed_channels"],
                max_concurrent_sessions=max(1, capacity),
                recording_allowed=plan["recording_allowed"],
            )
        except ValueError:
            logger.exception(
                "api_channel_capacity_sync_failed",
                extra={"tenant_id": key.tenant_id, "api_key_id": key.key_id},
            )


class AgentProviderConfigBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["calling", "post_call"]
    provider: Literal["vertex"] = "vertex"
    model: str = Field(min_length=1, max_length=160)
    location: str | None = Field(default=None, min_length=1, max_length=64)
    cost_per_minute_minor: int = Field(default=0, ge=0, le=1_000_000_000)
    currency: Literal["INR"] = "INR"
    auto_hangup_when_complete: bool = False
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("tags")
    @classmethod
    def normalize_tags_field(cls, value: list[str]) -> list[str]:
        return list(normalize_tags(value))

    @field_validator("name", "model")
    @classmethod
    def trim_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be blank")
        return value

    @field_validator("location")
    @classmethod
    def trim_location(cls, value: str | None) -> str | None:
        value = value.strip() if value is not None else None
        if value == "":
            raise ValueError("Location cannot be blank")
        return value


def _agent_config_directory(request: Request):
    directory = getattr(request.app.state, "agent_configuration_directory", None)
    if directory is None:
        raise HTTPException(status_code=503, detail="Agent configuration storage is not configured")
    return directory


async def _assigned_agent(request: Request, agent_id: str | None, tenant_id: str, kind: str) -> AgentConfiguration | None:
    if agent_id is None:
        return None
    agent = await _agent_config_directory(request).get(tenant_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"Selected platform {kind} agent was not found")
    if agent.kind != kind:
        raise HTTPException(status_code=422, detail=f"Selected agent must be configured as {kind}")
    return agent


def _agent_config_json(agent: AgentConfiguration) -> dict:
    return {
        "id": agent.agent_id, "owner": "platform",
        "name": agent.name, "kind": agent.kind,
        "provider": agent.provider, "model": agent.model, "location": agent.location,
        "cost_per_minute_minor": agent.cost_per_minute_minor, "currency": agent.currency,
        "auto_hangup_when_complete": agent.auto_hangup_when_complete,
        "tags": agent.tags,
        "enabled": agent.enabled, "created_at": agent.created_at.isoformat(),
        "updated_at": agent.updated_at.isoformat(),
    }


@router.get("/agent-configs")
async def list_agent_configs(request: Request, principal: Annotated[Principal, Depends(_platform_admin("platform:customers:read"))]):
    del principal
    agents = await _agent_config_directory(request).list_all()
    return {"items": [_agent_config_json(agent) for agent in agents]}


@router.post("/agent-configs", status_code=status.HTTP_201_CREATED)
async def create_agent_config(body: AgentProviderConfigBody, request: Request, principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))]):
    _accounts, _ = _stores(request)
    agent = AgentConfiguration(
        agent_id=uuid4().hex, tenant_id=PLATFORM_OWNER_ID, name=body.name.strip(),
        kind=body.kind, provider=body.provider, model=body.model.strip(),
        location=body.location or ("global" if body.kind == "post_call" else "us-central1"),
        cost_per_minute_minor=body.cost_per_minute_minor, currency=body.currency,
        auto_hangup_when_complete=body.auto_hangup_when_complete,
        tags=tuple(body.tags),
        enabled=True, created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    try:
        saved = await _agent_config_directory(request).create(agent)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="An agent with this name already exists for this type") from exc
    logger.info("agent_provider_config_created", extra={"actor_subject": principal.subject, "agent_id": saved.agent_id, "agent_kind": saved.kind})
    return _agent_config_json(saved)


@router.put("/agent-configs/{agent_id}")
async def update_agent_config(agent_id: str, body: AgentProviderConfigBody, request: Request, principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))]):
    directory = _agent_config_directory(request)
    current = await directory.get(PLATFORM_OWNER_ID, agent_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Agent configuration not found")
    replacement = AgentConfiguration(
        agent_id=agent_id, tenant_id=PLATFORM_OWNER_ID, name=body.name.strip(), kind=body.kind,
        provider=body.provider, model=body.model.strip(),
        location=body.location or ("global" if body.kind == "post_call" else "us-central1"),
        cost_per_minute_minor=body.cost_per_minute_minor, currency=body.currency,
        auto_hangup_when_complete=body.auto_hangup_when_complete,
        tags=tuple(body.tags),
        enabled=True, created_at=current.created_at, updated_at=datetime.now(UTC),
    )
    try:
        saved = await directory.update(replacement)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="An agent with this name already exists for this type") from exc
    if saved is None:
        raise HTTPException(status_code=404, detail="Agent configuration not found")
    return _agent_config_json(saved)


@router.delete("/agent-configs/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_config(
    agent_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    _account_store, api_key_directory = _stores(request)
    directory = _agent_config_directory(request)
    current = await directory.get(PLATFORM_OWNER_ID, agent_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Agent configuration not found")

    active_assignments = [
        key.key_id
        for key in await api_key_directory.list_all()
        if key.revoked_at is None and agent_id in (key.voice_agent_id, key.post_call_agent_id)
    ]
    if active_assignments:
        raise HTTPException(
            status_code=409,
            detail="This agent is assigned to an active API. Remove it from the API before deleting the agent.",
        )
    if not await directory.archive(PLATFORM_OWNER_ID, agent_id):
        raise HTTPException(status_code=404, detail="Agent configuration not found")
    logger.info(
        "agent_provider_config_archived",
        extra={"actor_subject": principal.subject, "agent_id": agent_id, "agent_kind": current.kind},
    )


def _validate_expiration(expires_at: datetime | None) -> datetime | None:
    if expires_at is None:
        return None
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise HTTPException(status_code=422, detail="API key expiry must include a timezone")
    expires_at = expires_at.astimezone(UTC)
    now = datetime.now(UTC)
    if expires_at <= now or expires_at > now + MAX_API_KEY_LIFETIME:
        raise HTTPException(
            status_code=422, detail="API key expiry must be within the next 365 days"
        )
    return expires_at


def _account_json(account: TenantAccount) -> dict:
    return {
        "customer_id": account.tenant_id,
        "display_name": account.display_name,
        "billing_mode": account.billing_mode.value,
        "created_at": account.created_at.isoformat(),
    }


def _key_json(key: ApiKeySummary) -> dict:
    return {
        "id": key.key_id,
        "name": key.name,
        "tags": key.tags,
        "key_prefix": key.key_prefix,
        "scopes": key.scopes,
        "created_by": key.created_by,
        "created_at": key.created_at.isoformat(),
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
        "customer_id": key.tenant_id,
        "assigned_numbers": [
            {"provider": provider, "phone_number": number}
            for provider, number in key.assigned_numbers
        ],
        "assigned_provider": key.assigned_numbers[0][0] if len(key.assigned_numbers) == 1 else None,
        "assigned_phone_number": (
            key.assigned_numbers[0][1] if len(key.assigned_numbers) == 1 else None
        ),
        "vertex_project_id": key.vertex_project_id,
        "vertex_location": key.vertex_location,
        "voice_location": key.voice_location,
        "post_call_location": key.post_call_location,
        "vertex_credentials_configured": key.vertex_credentials_configured,
        "voice_provider": key.voice_provider,
        "voice_model": key.voice_model,
        "post_call_provider": key.post_call_provider,
        "post_call_model": key.post_call_model,
        "voice_agent_enabled": key.voice_agent_enabled,
        "post_call_agent_enabled": key.post_call_agent_enabled,
        "voice_agent_id": key.voice_agent_id,
        "post_call_agent_id": key.post_call_agent_id,
    }


async def _request_vertex_project(request: Request, account: TenantAccount) -> dict | None:
    directory = getattr(request.app.state, "vertex_project_directory", None)
    if directory is None:
        return None
    try:
        return await directory.request_provisioning(account.tenant_id, account.display_name)
    except Exception as exc:
        # Account/key creation is already committed atomically. Do not lose the
        # one-time key response because a cloud project enqueue had a transient error.
        logger.error(
            "customer_vertex_project_enqueue_failed",
            extra={"tenant_id": account.tenant_id, "error_type": type(exc).__name__},
        )
        return {"status": "enqueue_failed"}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_customer(
    body: CreateCustomerBody,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    _account_store, api_key_directory = _stores(request)
    expires_at = _validate_expiration(body.api_key_expires_at)
    provision = getattr(api_key_directory, "provision_customer", None)
    if provision is None:
        raise HTTPException(status_code=503, detail="Atomic customer provisioning is unavailable")
    account, token, key = await provision(
        display_name=body.display_name,
        billing_mode=body.billing_mode,
        created_by=principal.subject,
        key_name=body.api_key_name,
        scopes=tuple(body.api_key_scopes),
        expires_at=expires_at,
    )
    logger.info(
        "customer_provisioned",
        extra={
            "actor_subject": principal.subject,
            "tenant_id": account.tenant_id,
            "api_key_id": key.key_id,
            "billing_mode": account.billing_mode.value,
        },
    )
    response = {
        **_account_json(account),
        "api_key": token,
        "api_key_record": _key_json(key),
        "message": "Copy this API key now; its secret will not be shown again.",
    }
    vertex_project = await _request_vertex_project(request, account)
    if vertex_project is not None:
        response["vertex_project"] = vertex_project
    return response


@router.get("")
async def list_customers(
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:read"))],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    del principal
    account_store, _ = _stores(request)
    items = await account_store.list(limit=limit, offset=offset)
    return {"items": [_account_json(account) for account in items]}


@router.get("/phone-numbers")
async def list_platform_phone_numbers(
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:read"))],
):
    del principal
    _, _ = _stores(request)
    records = await _integration_store(request).list_phone_numbers()
    api_keys = await _stores(request)[1].list_all()
    number_owners = {
        (provider, number): key
        for key in api_keys
        for provider, number in key.assigned_numbers
    }
    items = []
    vobiz_config_cache: dict[str, str | None] = {}
    for phone in records:
        credential_mode = None
        if phone.provider == "vobiz":
            if phone.tenant_id not in vobiz_config_cache:
                integration = await _integration_store(request).get("vobiz", phone.tenant_id)
                vobiz_config_cache[phone.tenant_id] = (
                    integration.settings.get("credential_mode") if integration else None
                )
            credential_mode = vobiz_config_cache[phone.tenant_id]
        owner = number_owners.get((phone.provider, phone.number))
        items.append(
            {
                "provider": phone.provider,
                "phone_number": phone.number,
                "customer_id": phone.tenant_id,
                "customer_name": "Platform",
                "inbound_enabled": phone.inbound_enabled,
                "outbound_enabled": phone.outbound_enabled,
                "standard_channels": phone.standard_channels,
                "unlimited_channels": phone.unlimited_channels,
                "tags": phone.tags,
                "agent_id": phone.agent_id,
                "api_key_id": owner.key_id if owner else None,
                "api_key_name": owner.name if owner else None,
                "credential_mode": credential_mode,
            }
        )
    return {"items": items}


@router.post("/phone-numbers", status_code=status.HTTP_201_CREATED)
async def add_platform_phone_numbers(
    body: AddPlatformNumbersBody,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    _account_store, _ = _stores(request)
    credentials = _vobiz_credentials(body.credential_mode, body.auth_id, body.auth_token)
    store = _integration_store(request)
    integration = await store.get("vobiz", PLATFORM_OWNER_ID)
    existing_numbers = list(integration.phone_numbers) if integration else []
    if integration and existing_numbers:
        current_mode = integration.settings.get("credential_mode", "custom")
        if current_mode != body.credential_mode:
            raise HTTPException(
                status_code=409,
                detail="Vobiz credential mode is shared by platform numbers. Edit the existing Vobiz configuration to change it.",
            )
    existing_values = {item.number for item in existing_numbers}
    if existing_values.intersection(body.phone_numbers):
        raise HTTPException(status_code=409, detail="A selected number is already registered")
    merged_numbers = [
        *existing_numbers,
        *(
            ProviderPhoneNumber(
                number,
                standard_channels=body.standard_channels,
                unlimited_channels=body.unlimited_channels,
                tags=tuple(body.tags),
            )
            for number in body.phone_numbers
        ),
    ]
    integration_settings = dict(integration.settings) if integration else {}
    integration_settings["credential_mode"] = body.credential_mode
    if integration_settings.get("default_caller_id") not in {
        item.number for item in merged_numbers if item.outbound_enabled
    }:
        integration_settings["default_caller_id"] = next(
            (item.number for item in merged_numbers if item.outbound_enabled), None
        )
    integration_settings = {key: value for key, value in integration_settings.items() if value is not None}
    try:
        await store.save(ProviderIntegration(
            provider="vobiz", tenant_id=PLATFORM_OWNER_ID, credentials=credentials,
            settings=integration_settings, phone_numbers=tuple(merged_numbers),
        ))
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="A phone number is already registered") from exc
    logger.info("platform_vobiz_numbers_added", extra={
        "actor_subject": principal.subject, "resource_owner": "platform",
        "phone_number_count": len(body.phone_numbers), "credential_mode": body.credential_mode,
    })
    return {"provider": "vobiz", "owner": "platform", "credential_mode": body.credential_mode,
            "phone_numbers": body.phone_numbers,
            "standard_channels": body.standard_channels,
            "unlimited_channels": body.unlimited_channels,
            "tags": body.tags}


@router.put("/phone-numbers/{provider}/{phone_number:path}")
async def update_platform_phone_number(
    provider: str,
    phone_number: str,
    body: UpdatePlatformNumberBody,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
    customer_id: str = Query(min_length=1, max_length=200),
):
    del principal
    if provider != "vobiz":
        raise HTTPException(status_code=404, detail="Provider is not supported for number management")
    store = _integration_store(request)
    integration = await store.get(provider, customer_id)
    if integration is None:
        raise HTTPException(status_code=404, detail="Provider configuration not found")
    numbers = [
        ProviderPhoneNumber(
            item.number, body.inbound_enabled, body.outbound_enabled, item.agent_id,
            body.standard_channels, body.unlimited_channels, tuple(body.tags),
        )
        if item.number == phone_number else item
        for item in integration.phone_numbers
    ]
    if len(numbers) == len(integration.phone_numbers):
        raise HTTPException(status_code=404, detail="Phone number not found")
    integration_settings = dict(integration.settings)
    outbound_numbers = {item.number for item in numbers if item.outbound_enabled}
    if integration_settings.get("default_caller_id") not in outbound_numbers:
        integration_settings["default_caller_id"] = next(iter(outbound_numbers), None)
    integration_settings = {key: value for key, value in integration_settings.items() if value is not None}
    await store.save(ProviderIntegration(provider, customer_id, integration.credentials, integration_settings, tuple(numbers)))
    await _sync_api_channel_capacity(request, (provider, phone_number))
    return {"status": "updated", "phone_number": phone_number, "tags": body.tags}


@router.delete("/phone-numbers/{provider}/{phone_number:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_platform_phone_number(
    provider: str,
    phone_number: str,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
    customer_id: str = Query(min_length=1, max_length=200),
):
    del principal
    if provider != "vobiz":
        raise HTTPException(status_code=404, detail="Provider is not supported for number management")
    store = _integration_store(request)
    integration = await store.get(provider, customer_id)
    if integration is None:
        raise HTTPException(status_code=404, detail="Provider configuration not found")
    numbers = tuple(item for item in integration.phone_numbers if item.number != phone_number)
    if len(numbers) == len(integration.phone_numbers):
        raise HTTPException(status_code=404, detail="Phone number not found")
    if not numbers:
        await store.delete(provider, customer_id)
        return
    integration_settings = dict(integration.settings)
    if integration_settings.get("default_caller_id") == phone_number:
        integration_settings["default_caller_id"] = next((item.number for item in numbers if item.outbound_enabled), None)
    integration_settings = {key: value for key, value in integration_settings.items() if value is not None}
    await store.save(ProviderIntegration(provider, customer_id, integration.credentials, integration_settings, numbers))


@router.get("/api-keys")
async def list_platform_api_keys(
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:read"))],
):
    del principal
    account_store, api_key_directory = _stores(request)
    items = []
    billing = getattr(request.app.state, "billing_directory", None)
    for key in await api_key_directory.list_all():
        account = await account_store.get(key.tenant_id)
        if account is None:
            continue
        plan = await billing.get_plan(key.tenant_id) if billing is not None else None
        items.append({
            **_key_json(key),
            "customer_name": account.display_name,
            "recording_allowed": bool(plan and plan.get("recording_allowed")),
        })
    return {"items": items}


@router.get("/api-keys/{key_id}/calls")
async def list_platform_api_calls(
    key_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:read"))],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    del principal
    _, api_keys = _stores(request)
    key = next((item for item in await api_keys.list_all() if item.key_id == key_id), None)
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    call_store = getattr(request.app.state, "call_session_store", None)
    if call_store is None or not hasattr(call_store, "list_for_api_key"):
        raise HTTPException(status_code=503, detail="Call history storage is not configured")
    result = await call_store.list_for_api_key(key.tenant_id, key_id, limit=limit, offset=offset)
    dispatcher = getattr(request.app.state, "post_call_dispatcher", None)
    if dispatcher is not None and hasattr(dispatcher, "get_status"):
        for call in result["items"]:
            if not call["post_call_agent_enabled"]:
                call["post_call_status"] = "disabled"
                call["post_call_result"] = None
                call["webhook_status"] = None
                continue
            post_call = await dispatcher.get_status(key.tenant_id, call["session_id"])
            call["post_call_status"] = post_call["status"] if post_call else (
                "failed" if call["status"] == "completed" and not call["transcript_available"]
                else "queued" if call["status"] == "completed" else None
            )
            call["post_call_result"] = post_call["result"] if post_call else None
            call["post_call_time_zone"] = post_call.get("time_zone") if post_call else None
            call["post_call_time_format"] = post_call.get("time_format") if post_call else None
            call["webhook_status"] = post_call["webhook_status"] if post_call else None
            call["post_call_error"] = (
                post_call.get("last_error_type") if post_call else
                "transcript_missing" if call["status"] == "completed" and not call["transcript_available"] else None
            )
    else:
        for call in result["items"]:
            if call["post_call_agent_enabled"] and call["status"] == "completed":
                call["post_call_status"] = "failed" if not call["transcript_available"] else "queued"
                call["post_call_error"] = "transcript_missing" if not call["transcript_available"] else "dispatcher_unavailable"
    return result


@router.get("/api-keys/{key_id}/voice-profiles")
async def list_api_voice_profiles(
    key_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:read"))],
):
    del principal
    _, api_keys = _stores(request)
    key = next((item for item in await api_keys.list_all() if item.key_id == key_id), None)
    if key is None or key.revoked_at is not None:
        raise HTTPException(status_code=404, detail="Active API key not found")
    directory = getattr(request.app.state, "agent_profile_directory", None)
    if directory is None:
        return {"items": []}
    profiles = await directory.list_for_tenant(key.tenant_id)
    return {"items": [
        {"id": profile.agent_id, "name": profile.name, "language": profile.language,
         "voice": profile.voice, "speaking_style": profile.speaking_style.value,
         "instructions": profile.instructions}
        for profile in profiles
    ]}


@router.post("/api-keys/{key_id}/simulate", status_code=status.HTTP_202_ACCEPTED)
async def simulate_api_call(
    key_id: str,
    body: OutboundCallBody,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    from chiefvoice.calls.coordinator import OutboundCallCommand

    _ = principal
    _, api_keys = _stores(request)
    key = next((item for item in await api_keys.list_all() if item.key_id == key_id), None)
    if key is None or key.revoked_at is not None or (key.expires_at and key.expires_at <= datetime.now(UTC)):
        raise HTTPException(status_code=404, detail="Active API key not found")
    if "calls:write" not in key.scopes:
        raise HTTPException(status_code=403, detail="Selected API does not have calls:write scope")
    if body.channel != "phone":
        raise HTTPException(status_code=422, detail="Simulation currently supports phone calls")
    provider, caller_id = resolve_assigned_sender(key.assigned_numbers, body.caller_id)
    live_template, post_call_template = await _resolve_call_templates(body, key.key_id, request)
    (
        live_instructions,
        live_context,
        _live_tools,
        template_hangup,
        live_language,
        completion_requirements,
    ) = _render_live(live_template, body.live_agent)
    post_context, post_schema, post_tools, post_time_zone, post_time_format, include_live_context = _render_post_call(post_call_template, body.post_call)
    coordinator = get_call_coordinator(request)
    try:
        session = await coordinator.start_outbound(OutboundCallCommand(
            provider=provider, channel=body.channel, tenant_id=key.tenant_id,
            api_key_id=key.key_id, destination=body.destination, caller_id=caller_id,
            bot_config_id=(body.live_agent.agent_id if body.live_agent else None),
            live_agent_instructions=live_instructions,
            live_agent_template_id=live_template.template_id if live_template else None,
            live_agent_template_revision=live_template.revision if live_template else None,
            live_agent_context=live_context,
            live_agent_language=live_language,
            live_agent_completion_requirements=completion_requirements,
            post_call_context=post_context,
            post_call_template_id=post_call_template.template_id if post_call_template else None,
            post_call_template_revision=post_call_template.revision if post_call_template else None,
            post_call_output_schema=post_schema,
            post_call_tools=post_tools,
            post_call_time_zone=post_time_zone,
            post_call_time_format=post_time_format,
            include_live_agent_context=include_live_context,
            auto_hangup_when_complete=template_hangup,
            recording_allowed=body.recording_allowed, idempotency_key=idempotency_key,
        ))
    except HTTPException as exc:
        call_start_event("rejected" if exc.status_code < 500 else "failed")
        raise
    except Exception:
        call_start_event("failed")
        raise
    call_start_event("accepted")
    return {"session_id": session.session_id, "provider": session.provider,
            "channel": session.channel, "direction": session.direction.value,
            "status": session.metadata.get("call_status", "starting")}


@router.get("/api-keys/{key_id}/billing")
async def get_platform_api_billing(
    key_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:read"))],
):
    del principal
    _, api_keys = _stores(request)
    key = next((item for item in await api_keys.list_all() if item.key_id == key_id), None)
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    billing = getattr(request.app.state, "billing_directory", None)
    if billing is None or not hasattr(billing, "get_usage_for_api_key"):
        raise HTTPException(status_code=503, detail="Billing storage is not configured")
    usage = await billing.get_usage_for_api_key(key.tenant_id, key_id)
    if usage is None:
        return {"configured": False, "api_key_id": key_id, "message": "No customer voice plan is configured."}
    return {"configured": True, "api_key_id": key_id, **usage}


@router.put("/api-keys/{key_id}/billing")
async def adjust_platform_api_billing(
    key_id: str,
    body: AdjustApiBillingBody,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    del principal
    account_store, api_keys = _stores(request)
    key = next((item for item in await api_keys.list_all() if item.key_id == key_id), None)
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    mode = await account_store.get_billing_mode(key.tenant_id)
    if mode is None:
        raise HTTPException(status_code=404, detail="API customer account was not found")
    if mode is BillingMode.PREPAID:
        if body.postpaid_credit_limit_minor is not None or body.prepaid_balance_minor is None:
            raise HTTPException(status_code=422, detail="Provide only prepaid_balance_minor for this API")
    elif body.prepaid_balance_minor is not None or body.postpaid_credit_limit_minor is None:
        raise HTTPException(status_code=422, detail="Provide only postpaid_credit_limit_minor for this API")
    billing = getattr(request.app.state, "billing_directory", None)
    if billing is None:
        raise HTTPException(status_code=503, detail="Billing storage is not configured")
    plan = await billing.get_plan(key.tenant_id)
    if plan is None:
        raise HTTPException(status_code=409, detail="Billing plan is not configured for this API")
    try:
        await billing.configure(
            tenant_id=key.tenant_id,
            billing_mode=mode,
            voice_enabled=plan["voice_enabled"],
            currency=plan["currency"],
            rate_per_minute_minor=plan["rate_per_minute_minor"],
            max_call_seconds=plan["max_call_seconds"],
            allowed_channels=plan["allowed_channels"],
            max_concurrent_sessions=plan["max_concurrent_sessions"],
            recording_allowed=plan["recording_allowed"],
            prepaid_balance_minor=body.prepaid_balance_minor,
            postpaid_credit_limit_minor=body.postpaid_credit_limit_minor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    usage = await billing.get_usage_for_api_key(key.tenant_id, key_id)
    return {"configured": usage is not None, "api_key_id": key_id, **(usage or {})}


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
async def issue_phone_api_key(
    body: CreatePhoneApiKeyBody,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    account_store, api_key_directory = _stores(request)
    if not body.voice_agent_id and not body.post_call_agent_id:
        raise HTTPException(status_code=422, detail="Select at least one agent for this API")
    requested = [(item.provider, item.phone_number) for item in body.phone_numbers]
    duplicate_pairs = len(requested) != len(set(requested))
    duplicate_numbers = len({number for _, number in requested}) != len(requested)
    if duplicate_pairs or duplicate_numbers:
        raise HTTPException(status_code=422, detail="Phone number assignments must be unique")
    registered = await _integration_store(request).list_phone_numbers()
    selected = [
        next(
            (
                item for item in registered
                if item.provider == provider and item.number == number and item.outbound_enabled
            ),
            None,
        )
        for provider, number in requested
    ]
    if any(item is None for item in selected):
        raise HTTPException(
            status_code=404, detail="An outbound-enabled phone number was not found"
        )
    channel_capacity = min(
        1_000,
        sum(item.standard_channels + item.unlimited_channels for item in selected if item),
    )
    if channel_capacity < 1:
        raise HTTPException(status_code=409, detail="Selected phone numbers have no configured channels")
    voice_agent = await _assigned_agent(request, body.voice_agent_id, PLATFORM_OWNER_ID, "calling")
    post_call_agent = await _assigned_agent(request, body.post_call_agent_id, PLATFORM_OWNER_ID, "post_call")
    webhook_directory = getattr(request.app.state, "post_call_webhook_directory", None)
    if body.post_call_webhook_url:
        if webhook_directory is None:
            raise HTTPException(status_code=503, detail="Webhook storage is not configured")
        try:
            await validate_public_webhook_url(body.post_call_webhook_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    expires_at = datetime.now(UTC) + MAX_API_KEY_LIFETIME
    credentials, credentials_json = _parse_gcp_service_account(body.gcp_credentials_json)
    if not settings.credentials_encryption_key:
        raise HTTPException(status_code=503, detail="Credential encryption is not configured")
    rate_per_minute_minor = sum(
        agent.cost_per_minute_minor for agent in (voice_agent, post_call_agent) if agent is not None
    )
    if rate_per_minute_minor < 1:
        raise HTTPException(status_code=422, detail="Selected agents must have a positive combined INR per-minute cost")
    if body.billing_mode is BillingMode.PREPAID and body.prepaid_balance_minor < 1:
        raise HTTPException(status_code=422, detail="Set a positive prepaid balance in INR")
    if body.billing_mode is BillingMode.POSTPAID and body.postpaid_credit_limit_minor < 1:
        raise HTTPException(status_code=422, detail="Set a positive postpaid credit limit in INR")
    try:
        provision = getattr(api_key_directory, "provision_customer", None)
        if provision is None:
            raise HTTPException(status_code=503, detail="Atomic API customer provisioning is unavailable")
        account, token, key = await provision(
            display_name=body.customer_name.strip(),
            billing_mode=body.billing_mode,
            created_by=principal.subject,
            key_name=body.customer_name.strip(),
            scopes=tuple(DEFAULT_CUSTOMER_API_KEY_SCOPES),
            expires_at=expires_at,
            assigned_numbers=tuple(requested),
            vertex_project_id=credentials["project_id"],
            vertex_location=body.voice_location or (voice_agent.location if voice_agent else None) or (post_call_agent.location if post_call_agent else None) or "us-central1",
            vertex_credentials_json=credentials_json,
            voice_location=body.voice_location or (voice_agent.location if voice_agent else None) or "us-central1",
            post_call_location=body.post_call_location or (post_call_agent.location if post_call_agent else None) or "global",
            voice_provider=voice_agent.provider if voice_agent else "vertex",
            voice_model=voice_agent.model if voice_agent else "google/gemini-live-2.5-flash-native-audio",
            post_call_provider=post_call_agent.provider if post_call_agent else "vertex",
            post_call_model=post_call_agent.model if post_call_agent else "gemini-3.5-flash-lite",
            voice_agent_enabled=voice_agent is not None,
            post_call_agent_enabled=post_call_agent is not None,
            voice_agent_id=voice_agent.agent_id if voice_agent else None,
            post_call_agent_id=post_call_agent.agent_id if post_call_agent else None,
            tags=tuple(body.tags),
            voice_plan={
                "voice_enabled": voice_agent is not None,
                "currency": "INR",
                "rate_per_minute_minor": rate_per_minute_minor,
                "max_call_seconds": 3600,
                "allowed_channels": ["phone"],
                "max_concurrent_sessions": channel_capacity,
                "recording_allowed": body.recording_allowed,
                "prepaid_balance_minor": body.prepaid_balance_minor,
                "postpaid_credit_limit_minor": body.postpaid_credit_limit_minor,
            },
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="A selected phone number is already assigned to another API"
        ) from exc
    tenant_id = account.tenant_id
    webhook_result = None
    webhook_setup_error = None
    if body.post_call_webhook_url:
        try:
            signing_secret, configured_at = await webhook_directory.configure(
                tenant_id, body.post_call_webhook_url
            )
            webhook_result = {
                "target_url": body.post_call_webhook_url,
                "signing_secret": signing_secret,
                "configured_at": configured_at.isoformat(),
            }
        except Exception:
            logger.exception(
                "api_post_call_webhook_configuration_failed",
                extra={"tenant_id": tenant_id, "api_key_id": key.key_id},
            )
            webhook_setup_error = (
                "The API was created, but webhook registration failed. Configure it later "
                "with PUT /v1/webhooks/post-call."
            )
    vertex_project = await _request_vertex_project(request, account)
    logger.info(
        "phone_api_key_issued",
        extra={
            "actor_subject": principal.subject,
            "tenant_id": tenant_id,
            "api_key_id": key.key_id,
            "phone_number_count": len(requested),
            "providers": sorted({provider for provider, _ in requested}),
        },
    )
    response = {
        "key": token,
        "post_call_webhook": webhook_result,
        "webhook_setup_error": webhook_setup_error,
        "api_key_record": {
            **_key_json(key),
            "customer_name": account.display_name,
            "recording_allowed": body.recording_allowed,
        },
        "message": "Copy this API key now; its secret will not be shown again.",
    }
    if vertex_project is not None:
        response["vertex_project"] = vertex_project
    return response


@router.put("/api-keys/{key_id}/config")
async def update_phone_api_key_config(
    key_id: str,
    body: UpdatePhoneApiKeyConfigBody,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    account_store, api_key_directory = _stores(request)
    if not body.voice_agent_id and not body.post_call_agent_id:
        raise HTTPException(status_code=422, detail="Select at least one agent for this API")
    key = next((item for item in await api_key_directory.list_all() if item.key_id == key_id), None)
    if key is None or key.revoked_at is not None:
        raise HTTPException(status_code=404, detail="Active API key not found")
    requested = [(item.provider, item.phone_number) for item in body.phone_numbers]
    if len(requested) != len(set(requested)) or len({number for _, number in requested}) != len(requested):
        raise HTTPException(status_code=422, detail="Phone number assignments must be unique")
    registered = await _integration_store(request).list_phone_numbers()
    selected_phone_numbers = []
    for provider, number in requested:
        phone = next((item for item in registered if item.provider == provider and item.number == number and item.outbound_enabled), None)
        if phone is None:
            raise HTTPException(status_code=404, detail="An outbound-enabled platform number was not found")
        selected_phone_numbers.append(phone)
    channel_capacity = min(
        1_000,
        sum(item.standard_channels + item.unlimited_channels for item in selected_phone_numbers),
    )
    if channel_capacity < 1:
        raise HTTPException(status_code=409, detail="Selected phone numbers have no configured channels")
    voice_agent = await _assigned_agent(request, body.voice_agent_id, PLATFORM_OWNER_ID, "calling")
    post_call_agent = await _assigned_agent(request, body.post_call_agent_id, PLATFORM_OWNER_ID, "post_call")
    credentials_json = None
    project_id = key.vertex_project_id
    if body.gcp_credentials_json and body.gcp_credentials_json.strip():
        credentials, credentials_json = _parse_gcp_service_account(body.gcp_credentials_json)
        project_id = credentials["project_id"]
    if not project_id or (not key.vertex_credentials_configured and credentials_json is None):
        raise HTTPException(status_code=422, detail="GCP service account JSON is required for this API")
    billing = getattr(request.app.state, "billing_directory", None)
    current_plan = await billing.get_plan(key.tenant_id) if billing is not None else None
    recording_allowed = (
        body.recording_allowed
        if body.recording_allowed is not None
        else bool(current_plan and current_plan.get("recording_allowed"))
    )
    try:
        replaced = await api_key_directory.update_config_and_numbers(
            key.tenant_id, key_id,
            name=body.name.strip(),
            assigned_numbers=tuple(requested),
            vertex_project_id=project_id,
            vertex_location=body.voice_location or (voice_agent.location if voice_agent else None) or (body.post_call_location or (post_call_agent.location if post_call_agent else None)) or key.vertex_location or "us-central1",
            vertex_credentials_json=credentials_json,
            voice_location=body.voice_location or (voice_agent.location if voice_agent else None) or key.voice_location or "us-central1",
            post_call_location=body.post_call_location or (post_call_agent.location if post_call_agent else None) or key.post_call_location or "global",
            voice_provider=voice_agent.provider if voice_agent else "vertex",
            voice_model=voice_agent.model if voice_agent else "google/gemini-live-2.5-flash-native-audio",
            post_call_provider=post_call_agent.provider if post_call_agent else "vertex",
            post_call_model=post_call_agent.model if post_call_agent else "gemini-3.5-flash-lite",
            voice_agent_enabled=voice_agent is not None,
            post_call_agent_enabled=post_call_agent is not None,
            voice_agent_id=voice_agent.agent_id if voice_agent else None,
            post_call_agent_id=post_call_agent.agent_id if post_call_agent else None,
            tags=tuple(body.tags),
        )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="A selected phone number is already assigned to another API") from exc
    if not replaced:
        raise HTTPException(status_code=404, detail="Active API key not found")
    # The API's customer price is a snapshot of its currently assigned templates.
    # Updating the assignment changes future reservations while preserving the
    # customer's balance, credit usage, and billing history.
    if billing is not None:
        plan = current_plan
        if plan is not None:
            rate_per_minute_minor = sum(
                agent.cost_per_minute_minor
                for agent in (voice_agent, post_call_agent)
                if agent is not None
            )
            try:
                await billing.configure(
                    tenant_id=key.tenant_id,
                    billing_mode=BillingMode(plan["billing_mode"]),
                    voice_enabled=voice_agent is not None,
                    currency=plan["currency"],
                    rate_per_minute_minor=rate_per_minute_minor,
                    max_call_seconds=plan["max_call_seconds"],
                    allowed_channels=plan["allowed_channels"],
                    max_concurrent_sessions=channel_capacity,
                    recording_allowed=recording_allowed,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
    refreshed = next(item for item in await api_key_directory.list_for_tenant(key.tenant_id) if item.key_id == key_id)
    account = await account_store.get(key.tenant_id)
    logger.info("platform_api_config_updated", extra={"actor_subject": principal.subject, "api_key_id": key_id, "tenant_id": key.tenant_id})
    return {
        **_key_json(refreshed),
        "customer_name": account.display_name if account else "",
        "recording_allowed": recording_allowed,
    }


@router.put("/api-keys/{key_id}/phone-numbers")
async def replace_phone_api_key_numbers(
    key_id: str,
    body: ReplacePhoneApiKeyNumbersBody,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    del principal
    account_store, api_key_directory = _stores(request)
    key = next((item for item in await api_key_directory.list_all() if item.key_id == key_id), None)
    if key is None or key.revoked_at is not None:
        raise HTTPException(status_code=404, detail="Active API key not found")
    requested = [(item.provider, item.phone_number) for item in body.phone_numbers]
    duplicate_pairs = len(requested) != len(set(requested))
    duplicate_numbers = len({number for _, number in requested}) != len(requested)
    if duplicate_pairs or duplicate_numbers:
        raise HTTPException(status_code=422, detail="Phone number assignments must be unique")
    registered = await _integration_store(request).list_phone_numbers()
    for provider, number in requested:
        phone = next(
            (
                item for item in registered
                if item.provider == provider
                and item.number == number
                and item.outbound_enabled
            ),
            None,
        )
        if phone is None:
            raise HTTPException(
                status_code=404, detail="An outbound-enabled platform number was not found"
            )
    try:
        replaced = await api_key_directory.replace_phone_numbers(
            key.tenant_id, key.key_id, tuple(requested)
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="A selected phone number is already assigned to another API"
        ) from exc
    if not replaced:
        raise HTTPException(status_code=404, detail="Active API key not found")
    account = await account_store.get(key.tenant_id)
    refreshed = next(
        item
        for item in await api_key_directory.list_for_tenant(key.tenant_id)
        if item.key_id == key.key_id
    )
    return {**_key_json(refreshed), "customer_name": account.display_name if account else ""}


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_platform_api_key(
    key_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    _account_store, api_key_directory = _stores(request)
    key = next((item for item in await api_key_directory.list_all() if item.key_id == key_id), None)
    if key is None or not await api_key_directory.delete_permanently(key.tenant_id, key.key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    logger.info(
        "platform_api_key_deleted",
        extra={
            "actor_subject": principal.subject,
            "tenant_id": key.tenant_id,
            "api_key_id": key_id,
        },
    )


@router.get("/{customer_id}/api-keys")
async def list_customer_api_keys(
    customer_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:read"))],
):
    account_store, api_key_directory = _stores(request)
    if await account_store.get(customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    keys = await api_key_directory.list_for_tenant(customer_id)
    return {"items": [_key_json(key) for key in keys]}


@router.post("/{customer_id}/api-keys", status_code=status.HTTP_201_CREATED)
async def issue_customer_api_key(
    customer_id: str,
    body: CreateCustomerApiKeyBody,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    account_store, api_key_directory = _stores(request)
    account = await account_store.get(customer_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    try:
        token, key = await api_key_directory.create(
            tenant_id=customer_id,
            name=body.name,
            scopes=tuple(body.scopes),
            created_by=principal.subject,
            expires_at=_validate_expiration(body.expires_at),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Customer not found") from exc
    logger.info(
        "customer_api_key_issued",
        extra={
            "actor_subject": principal.subject,
            "tenant_id": customer_id,
            "api_key_id": key.key_id,
        },
    )
    return {
        "key": token,
        "api_key_record": _key_json(key),
        "message": "Copy this API key now; its secret will not be shown again.",
    }


@router.delete("/{customer_id}/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_customer_api_key(
    customer_id: str,
    key_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    account_store, api_key_directory = _stores(request)
    if await account_store.get(customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    if not await api_key_directory.revoke(customer_id, key_id):
        raise HTTPException(status_code=404, detail="API key not found")
    logger.info(
        "customer_api_key_revoked",
        extra={
            "actor_subject": principal.subject,
            "tenant_id": customer_id,
            "api_key_id": key_id,
        },
    )
