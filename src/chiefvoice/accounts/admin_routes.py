"""Platform-admin customer provisioning and credential lifecycle APIs."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security, status
from pydantic import BaseModel, Field, field_validator

from chiefvoice.accounts.models import AccountStore, TenantAccount
from chiefvoice.billing import BillingMode
from chiefvoice.channels.integration_store import SqlProviderIntegrationStore
from chiefvoice.core.config import settings
from chiefvoice.identity.api_keys import ApiKeyDirectory, ApiKeySummary
from chiefvoice.identity.auth import Principal, current_principal

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
DEFAULT_CUSTOMER_API_KEY_SCOPES = ["calls:read", "calls:write"]
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


class CreatePhoneApiKeyBody(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    phone_number: str = Field(min_length=5, max_length=64)


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
        "key_prefix": key.key_prefix,
        "scopes": key.scopes,
        "created_by": key.created_by,
        "created_at": key.created_at.isoformat(),
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
        "customer_id": key.tenant_id,
        "assigned_provider": key.assigned_provider,
        "assigned_phone_number": key.assigned_phone_number,
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
    account_store, _ = _stores(request)
    records = await _integration_store(request).list_phone_numbers()
    items = []
    account_cache: dict[str, TenantAccount | None] = {}
    for phone in records:
        if phone.tenant_id not in account_cache:
            account_cache[phone.tenant_id] = await account_store.get(phone.tenant_id)
        account = account_cache[phone.tenant_id]
        if account is None:
            continue
        items.append(
            {
                "provider": phone.provider,
                "phone_number": phone.number,
                "customer_id": phone.tenant_id,
                "customer_name": account.display_name,
                "inbound_enabled": phone.inbound_enabled,
                "outbound_enabled": phone.outbound_enabled,
                "agent_id": phone.agent_id,
            }
        )
    return {"items": items}


@router.get("/api-keys")
async def list_platform_api_keys(
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:read"))],
):
    del principal
    account_store, api_key_directory = _stores(request)
    items = []
    for key in await api_key_directory.list_all():
        account = await account_store.get(key.tenant_id)
        if account is None:
            continue
        items.append({**_key_json(key), "customer_name": account.display_name})
    return {"items": items}


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
async def issue_phone_api_key(
    body: CreatePhoneApiKeyBody,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    account_store, api_key_directory = _stores(request)
    phone = next(
        (
            item
            for item in await _integration_store(request).list_phone_numbers()
            if item.provider == body.provider
            and item.number == body.phone_number
            and item.outbound_enabled
        ),
        None,
    )
    if phone is None:
        raise HTTPException(status_code=404, detail="Outbound-enabled phone number was not found")
    account = await account_store.get(phone.tenant_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Phone number customer was not found")
    expires_at = datetime.now(UTC) + MAX_API_KEY_LIFETIME
    token, key = await api_key_directory.create(
        tenant_id=phone.tenant_id,
        name=f"API for {phone.number}",
        scopes=tuple(DEFAULT_CUSTOMER_API_KEY_SCOPES),
        created_by=principal.subject,
        expires_at=expires_at,
        assigned_provider=phone.provider,
        assigned_phone_number=phone.number,
    )
    logger.info(
        "phone_api_key_issued",
        extra={
            "actor_subject": principal.subject,
            "tenant_id": phone.tenant_id,
            "api_key_id": key.key_id,
            "provider": phone.provider,
            "phone_number": phone.number,
        },
    )
    return {
        "key": token,
        "api_key_record": {**_key_json(key), "customer_name": account.display_name},
        "message": "Copy this API key now; its secret will not be shown again.",
    }


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_platform_api_key(
    key_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    _account_store, api_key_directory = _stores(request)
    key = next((item for item in await api_key_directory.list_all() if item.key_id == key_id), None)
    if key is None or not await api_key_directory.revoke(key.tenant_id, key.key_id):
        raise HTTPException(status_code=404, detail="API key not found or already revoked")
    logger.info(
        "platform_api_key_revoked",
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
