"""Platform-admin customer provisioning and credential lifecycle APIs."""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError

from chiefvoice.accounts.models import AccountStore, TenantAccount
from chiefvoice.billing import BillingMode
from chiefvoice.channels.integration_store import SqlProviderIntegrationStore
from chiefvoice.channels.integrations import ProviderIntegration, ProviderPhoneNumber
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


class PhoneNumberAssignment(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    phone_number: str = Field(min_length=5, max_length=64)


class CreatePhoneApiKeyBody(BaseModel):
    phone_numbers: list[PhoneNumberAssignment] = Field(min_length=1, max_length=100)
    gcp_credentials_json: str
    vertex_location: str = Field(default="us-central1", min_length=1, max_length=64)
    voice_provider: str = "vertex"
    voice_model: str = Field(default="google/gemini-live-2.5-flash-native-audio", min_length=1, max_length=160)
    post_call_provider: str = "vertex"
    post_call_model: str = Field(default="gemini-2.5-flash", min_length=1, max_length=160)

    @field_validator("vertex_location")
    @classmethod
    def clean_location(cls, value: str) -> str:
        return value.strip()

    @field_validator("voice_provider", "post_call_provider")
    @classmethod
    def supported_provider(cls, value: str) -> str:
        if value.strip().lower() != "vertex":
            raise ValueError("Only Vertex AI is currently supported")
        return "vertex"

class UpdatePhoneApiKeyConfigBody(BaseModel):
    phone_numbers: list[PhoneNumberAssignment] = Field(min_length=1, max_length=100)
    vertex_location: str = Field(default="us-central1", min_length=1, max_length=64)
    gcp_credentials_json: str | None = None
    voice_provider: str = "vertex"
    voice_model: str = Field(default="google/gemini-live-2.5-flash-native-audio", min_length=1, max_length=160)
    post_call_provider: str = "vertex"
    post_call_model: str = Field(default="gemini-2.5-flash", min_length=1, max_length=160)

    @field_validator("vertex_location")
    @classmethod
    def clean_location(cls, value: str) -> str:
        return value.strip()

    @field_validator("voice_provider", "post_call_provider")
    @classmethod
    def supported_provider(cls, value: str) -> str:
        if value.strip().lower() != "vertex":
            raise ValueError("Only Vertex AI is currently supported")
        return "vertex"



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
    customer_id: str = Field(min_length=1, max_length=200)
    credential_mode: Literal["platform", "custom"]
    phone_numbers: list[str] = Field(min_length=1, max_length=100)
    auth_id: str | None = Field(default=None, max_length=200)
    auth_token: str | None = Field(default=None, max_length=2000)

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
        "vertex_credentials_configured": key.vertex_credentials_configured,
        "voice_provider": key.voice_provider,
        "voice_model": key.voice_model,
        "post_call_provider": key.post_call_provider,
        "post_call_model": key.post_call_model,
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
    api_keys = await _stores(request)[1].list_all()
    number_owners = {
        number: key
        for key in api_keys
        for _, number in key.assigned_numbers
    }
    items = []
    account_cache: dict[str, TenantAccount | None] = {}
    vobiz_config_cache: dict[str, str | None] = {}
    for phone in records:
        if phone.tenant_id not in account_cache:
            account_cache[phone.tenant_id] = await account_store.get(phone.tenant_id)
        account = account_cache[phone.tenant_id]
        if account is None:
            continue
        credential_mode = None
        if phone.provider == "vobiz":
            if phone.tenant_id not in vobiz_config_cache:
                integration = await _integration_store(request).get("vobiz", phone.tenant_id)
                vobiz_config_cache[phone.tenant_id] = (
                    integration.settings.get("credential_mode") if integration else None
                )
            credential_mode = vobiz_config_cache[phone.tenant_id]
        owner = number_owners.get(phone.number)
        items.append(
            {
                "provider": phone.provider,
                "phone_number": phone.number,
                "customer_id": phone.tenant_id,
                "customer_name": account.display_name,
                "inbound_enabled": phone.inbound_enabled,
                "outbound_enabled": phone.outbound_enabled,
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
    account_store, _ = _stores(request)
    account = await account_store.get(body.customer_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    credentials = _vobiz_credentials(body.credential_mode, body.auth_id, body.auth_token)
    store = _integration_store(request)
    integration = await store.get("vobiz", body.customer_id)
    existing_numbers = list(integration.phone_numbers) if integration else []
    if integration and existing_numbers:
        current_mode = integration.settings.get("credential_mode", "custom")
        if current_mode != body.credential_mode:
            raise HTTPException(
                status_code=409,
                detail="Vobiz credential mode is shared by this customer’s numbers. Edit the existing Vobiz configuration to change it.",
            )
    existing_values = {item.number for item in existing_numbers}
    if existing_values.intersection(body.phone_numbers):
        raise HTTPException(status_code=409, detail="A selected number is already registered for this customer")
    merged_numbers = [*existing_numbers, *(ProviderPhoneNumber(number) for number in body.phone_numbers)]
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
            provider="vobiz", tenant_id=body.customer_id, credentials=credentials,
            settings=integration_settings, phone_numbers=tuple(merged_numbers),
        ))
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="A phone number is already registered") from exc
    logger.info("platform_vobiz_numbers_added", extra={
        "actor_subject": principal.subject, "tenant_id": body.customer_id,
        "phone_number_count": len(body.phone_numbers), "credential_mode": body.credential_mode,
    })
    return {"provider": "vobiz", "customer_id": body.customer_id, "credential_mode": body.credential_mode,
            "phone_numbers": body.phone_numbers}


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
        ProviderPhoneNumber(item.number, body.inbound_enabled, body.outbound_enabled, item.agent_id)
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
    return {"status": "updated", "phone_number": phone_number}


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
    for key in await api_key_directory.list_all():
        account = await account_store.get(key.tenant_id)
        if account is None:
            continue
        items.append({**_key_json(key), "customer_name": account.display_name})
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
            post_call = await dispatcher.get_status(key.tenant_id, call["session_id"])
            call["post_call_status"] = post_call["status"] if post_call else None
            call["post_call_result"] = post_call["result"] if post_call else None
            call["webhook_status"] = post_call["webhook_status"] if post_call else None
    return result


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


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
async def issue_phone_api_key(
    body: CreatePhoneApiKeyBody,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    account_store, api_key_directory = _stores(request)
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
    tenant_ids = {item.tenant_id for item in selected if item is not None}
    if len(tenant_ids) != 1:
        raise HTTPException(status_code=422, detail="All phone numbers must belong to one customer")
    tenant_id = next(iter(tenant_ids))
    credentials, credentials_json = _parse_gcp_service_account(body.gcp_credentials_json)
    if not settings.credentials_encryption_key:
        raise HTTPException(status_code=503, detail="Encrypted GCP credential storage is not configured")
    account = await account_store.get(tenant_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Phone number customer was not found")
    expires_at = datetime.now(UTC) + MAX_API_KEY_LIFETIME
    try:
        token, key = await api_key_directory.create(
            tenant_id=tenant_id,
            name=f"API for {len(requested)} phone number{'s' if len(requested) != 1 else ''}",
            scopes=tuple(DEFAULT_CUSTOMER_API_KEY_SCOPES),
            created_by=principal.subject,
            expires_at=expires_at,
            assigned_numbers=tuple(requested),
            vertex_project_id=credentials["project_id"],
            vertex_location=body.vertex_location,
            vertex_credentials_json=credentials_json,
            voice_provider=body.voice_provider,
            voice_model=body.voice_model.strip(),
            post_call_provider=body.post_call_provider,
            post_call_model=body.post_call_model.strip(),
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="A selected phone number is already assigned to another API"
        ) from exc
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
    return {
        "key": token,
        "api_key_record": {**_key_json(key), "customer_name": account.display_name},
        "message": "Copy this API key now; its secret will not be shown again.",
    }


@router.put("/api-keys/{key_id}/config")
async def update_phone_api_key_config(
    key_id: str,
    body: UpdatePhoneApiKeyConfigBody,
    request: Request,
    principal: Annotated[Principal, Depends(_platform_admin("platform:customers:write"))],
):
    account_store, api_key_directory = _stores(request)
    key = next((item for item in await api_key_directory.list_all() if item.key_id == key_id), None)
    if key is None or key.revoked_at is not None:
        raise HTTPException(status_code=404, detail="Active API key not found")
    requested = [(item.provider, item.phone_number) for item in body.phone_numbers]
    if len(requested) != len(set(requested)) or len({number for _, number in requested}) != len(requested):
        raise HTTPException(status_code=422, detail="Phone number assignments must be unique")
    registered = await _integration_store(request).list_phone_numbers()
    for provider, number in requested:
        phone = next((item for item in registered if item.tenant_id == key.tenant_id and item.provider == provider and item.number == number and item.outbound_enabled), None)
        if phone is None:
            raise HTTPException(status_code=404, detail="An outbound-enabled number for this customer was not found")
    credentials_json = body.gcp_credentials_json
    if credentials_json is not None:
        if not settings.credentials_encryption_key:
            raise HTTPException(status_code=503, detail="Encrypted GCP credential storage is not configured")
        credentials, credentials_json = _parse_gcp_service_account(credentials_json)
        project_id = credentials["project_id"]
    else:
        project_id = key.vertex_project_id
        if not key.vertex_credentials_configured or not project_id:
            raise HTTPException(status_code=422, detail="GCP credentials are required for this API")
    try:
        replaced = await api_key_directory.update_config_and_numbers(
            key.tenant_id, key_id,
            assigned_numbers=tuple(requested),
            vertex_project_id=project_id,
            vertex_location=body.vertex_location,
            vertex_credentials_json=credentials_json,
            voice_provider=body.voice_provider,
            voice_model=body.voice_model.strip(),
            post_call_provider=body.post_call_provider,
            post_call_model=body.post_call_model.strip(),
        )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="A selected phone number is already assigned to another API") from exc
    if not replaced:
        raise HTTPException(status_code=404, detail="Active API key not found")
    refreshed = next(item for item in await api_key_directory.list_for_tenant(key.tenant_id) if item.key_id == key_id)
    account = await account_store.get(key.tenant_id)
    logger.info("platform_api_config_updated", extra={"actor_subject": principal.subject, "api_key_id": key_id, "tenant_id": key.tenant_id})
    return {**_key_json(refreshed), "customer_name": account.display_name if account else ""}


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
                if item.tenant_id == key.tenant_id
                and item.provider == provider
                and item.number == number
                and item.outbound_enabled
            ),
            None,
        )
        if phone is None:
            raise HTTPException(
                status_code=404, detail="An outbound-enabled number for this customer was not found"
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
