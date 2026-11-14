from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Security, status
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy.exc import IntegrityError

from chiefvoice.channels.integration_store import SqlProviderIntegrationStore
from chiefvoice.channels.integrations import ProviderIntegration, ProviderPhoneNumber
from chiefvoice.identity.auth import Principal, require_scope

router = APIRouter(prefix="/v1/integrations", tags=["integrations"])
SUPPORTED_PROVIDERS = frozenset({"vobiz", "telecmi", "piopiy", "twilio"})


class PhoneNumberBody(BaseModel):
    number: str = Field(min_length=5, max_length=64)
    inbound_enabled: bool = True
    outbound_enabled: bool = True
    agent_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("number")
    @classmethod
    def normalize_number(cls, value: str) -> str:
        return value.strip()


class ProviderIntegrationBody(BaseModel):
    credentials: dict[str, SecretStr] = Field(min_length=1)
    settings: dict[str, str | int | bool | None] = Field(default_factory=dict)
    phone_numbers: list[PhoneNumberBody] = Field(default_factory=list, max_length=500)


def _store(request: Request) -> SqlProviderIntegrationStore:
    store = getattr(request.app.state, "provider_integration_store", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="Provider integration storage is not configured"
        )
    return store


def _validate_provider(provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=404, detail="Provider integration is not supported")


@router.get("/{provider}")
async def get_integration(
    provider: str,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("integrations:read"))],
):
    _validate_provider(provider)
    integration = await _store(request).get(provider, principal.tenant_id)
    if integration is None:
        raise HTTPException(status_code=404, detail="Provider is not configured for this account")
    phone_numbers = integration.phone_numbers
    integration_settings = integration.settings
    if principal.auth_method == "api_key":
        if not any(
            assigned_provider == provider
            for assigned_provider, _ in principal.assigned_numbers
        ):
            raise HTTPException(
                status_code=403, detail="This API key is not assigned a number for this provider"
            )
        phone_numbers = tuple(
            number
            for number in phone_numbers
            if (provider, number.number) in principal.assigned_numbers
        )
        if integration_settings.get("default_caller_id") not in {
            number for assigned_provider, number in principal.assigned_numbers
            if assigned_provider == provider
        }:
            integration_settings = {
                key: value
                for key, value in integration_settings.items()
                if key != "default_caller_id"
            }
    return {
        "provider": integration.provider,
        "credentials_configured": sorted(integration.credentials.keys()),
        "settings": integration_settings,
        "phone_numbers": [
            {
                "number": n.number,
                "inbound_enabled": n.inbound_enabled,
                "outbound_enabled": n.outbound_enabled,
                "agent_id": n.agent_id,
            }
            for n in phone_numbers
        ],
    }


@router.put("/{provider}")
async def put_integration(
    provider: str,
    body: ProviderIntegrationBody,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("integrations:write"))],
):
    _validate_provider(provider)
    numbers = tuple(
        ProviderPhoneNumber(n.number, n.inbound_enabled, n.outbound_enabled, n.agent_id)
        for n in body.phone_numbers
    )
    if len({n.number for n in numbers}) != len(numbers):
        raise HTTPException(status_code=422, detail="Phone numbers must be unique")
    requested_agent_ids = {number.agent_id for number in numbers if number.agent_id}
    default_agent_id = body.settings.get("default_agent_id")
    if default_agent_id is not None:
        if not isinstance(default_agent_id, str) or not default_agent_id.strip():
            raise HTTPException(
                status_code=422, detail="default_agent_id must be a nonempty string"
            )
        requested_agent_ids.add(default_agent_id)
    if requested_agent_ids:
        profile_directory = getattr(request.app.state, "agent_profile_directory", None)
        if profile_directory is None:
            raise HTTPException(status_code=503, detail="Agent profile storage is not configured")
        for agent_id in requested_agent_ids:
            if await profile_directory.get(principal.tenant_id, agent_id) is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Agent profile '{agent_id}' does not belong to this customer "
                        "or is unavailable"
                    ),
                )
    credentials = {key: value.get_secret_value() for key, value in body.credentials.items()}
    if provider == "vobiz":
        if not credentials.get("auth_id") or not credentials.get("auth_token"):
            raise HTTPException(
                status_code=422,
                detail="Vobiz requires auth_id and auth_token credentials",
            )
        default_caller_id = body.settings.get("default_caller_id")
        if not isinstance(default_caller_id, str) or default_caller_id not in {
            n.number for n in numbers if n.outbound_enabled
        }:
            raise HTTPException(
                status_code=422,
                detail="Vobiz default_caller_id must be an outbound-enabled phone number",
            )
    try:
        await _store(request).save(
            ProviderIntegration(
                provider=provider,
                tenant_id=principal.tenant_id,
                credentials=credentials,
                settings=body.settings,
                phone_numbers=numbers,
            )
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail="A phone number is already assigned for this provider",
        ) from exc
    return {"status": "configured", "provider": provider}


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_integration(
    provider: str,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("integrations:write"))],
):
    _validate_provider(provider)
    await _store(request).delete(provider, principal.tenant_id)
