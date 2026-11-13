from datetime import UTC, datetime, timedelta
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from pydantic import BaseModel, Field, field_validator

from chiefvoice.identity.api_keys import ApiKeyDirectory
from chiefvoice.identity.auth import Principal, current_principal

router = APIRouter(prefix="/v1/api-keys", tags=["API keys"])
_SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9:_-]{0,79}$")
_NEVER_DELEGATE = frozenset({"api_keys:read", "api_keys:write", "accounts:create"})
_MAX_KEY_LIFETIME = timedelta(days=365)


class CreateApiKeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1, max_length=50)
    expires_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name cannot be blank")
        return value

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, scopes: list[str]) -> list[str]:
        cleaned = [scope.strip() for scope in scopes]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Scopes must be unique")
        if any(not _SCOPE_PATTERN.fullmatch(scope) for scope in cleaned):
            raise ValueError("Scopes must use lowercase scope names")
        return cleaned


def _directory(request: Request) -> ApiKeyDirectory:
    directory: ApiKeyDirectory | None = getattr(request.app.state, "api_key_directory", None)
    if directory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API key storage is not configured",
        )
    return directory


def _oidc_scope(required_scope: str):
    async def dependency(
        principal: Annotated[Principal, Security(current_principal)],
    ) -> Principal:
        if principal.auth_method != "oidc":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API keys cannot manage API keys",
            )
        if required_scope not in principal.scopes:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient scope")
        return principal

    return dependency


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: CreateApiKeyBody,
    request: Request,
    principal: Annotated[Principal, Depends(_oidc_scope("api_keys:write"))],
):
    requested_scopes = set(body.scopes)
    forbidden = requested_scopes & _NEVER_DELEGATE
    if forbidden:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"These scopes cannot be assigned to API keys: {', '.join(sorted(forbidden))}",
        )
    if not requested_scopes.issubset(principal.scopes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key scopes must be a subset of your own scopes",
        )
    now = datetime.now(UTC)
    expires_at = body.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise HTTPException(status_code=422, detail="expires_at must include a timezone")
        expires_at = expires_at.astimezone(UTC)
        if expires_at <= now or expires_at > now + _MAX_KEY_LIFETIME:
            raise HTTPException(
                status_code=422,
                detail="expires_at must be in the future and within 365 days",
            )
    token, key = await _directory(request).create(
        tenant_id=principal.tenant_id,
        name=body.name,
        scopes=tuple(sorted(requested_scopes)),
        created_by=principal.subject,
        expires_at=expires_at,
    )
    return {
        "id": key.key_id,
        "name": key.name,
        "key": token,
        "key_prefix": key.key_prefix,
        "scopes": key.scopes,
        "created_at": key.created_at.isoformat(),
        "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        "message": "Copy this key now; it will not be shown again.",
    }


@router.get("")
async def list_api_keys(
    request: Request,
    principal: Annotated[Principal, Depends(_oidc_scope("api_keys:read"))],
):
    keys = await _directory(request).list_for_tenant(principal.tenant_id)
    return {
        "items": [
            {
                "id": key.key_id,
                "name": key.name,
                "key_prefix": key.key_prefix,
                "scopes": key.scopes,
                "created_by": key.created_by,
                "created_at": key.created_at.isoformat(),
                "expires_at": key.expires_at.isoformat() if key.expires_at else None,
                "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
                "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
            }
            for key in keys
        ]
    }


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(_oidc_scope("api_keys:write"))],
):
    revoked = await _directory(request).revoke(principal.tenant_id, key_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="API key was not found or is already revoked")
