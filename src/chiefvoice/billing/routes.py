import re
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Security
from pydantic import BaseModel, ConfigDict, Field, field_validator

from chiefvoice.billing.models import BillingMode
from chiefvoice.identity.auth import Principal, require_scope

router = APIRouter(prefix="/v1/billing", tags=["billing"])


class ConfigureVoicePlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice_enabled: bool
    currency: str = Field(min_length=3, max_length=3)
    rate_per_minute_minor: int = Field(ge=1, le=1_000_000_000)
    max_call_seconds: int = Field(default=300, ge=1, le=3_600)
    allowed_channels: list[str] = Field(min_length=1, max_length=20)
    max_concurrent_sessions: int = Field(default=1, ge=1, le=1_000)
    recording_allowed: bool = False
    prepaid_balance_minor: int | None = Field(default=None, ge=0, le=10**15)
    postpaid_credit_limit_minor: int | None = Field(default=None, ge=0, le=10**15)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        value = value.upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("currency must be a three-letter ISO code")
        return value

    @field_validator("allowed_channels")
    @classmethod
    def validate_channels(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().lower() for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed_channels must not contain duplicates")
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", value) for value in normalized):
            raise ValueError("allowed_channels contains an invalid channel name")
        return normalized


def _directory(request: Request):
    directory = getattr(request.app.state, "billing_directory", None)
    if directory is None:
        raise HTTPException(status_code=503, detail="Billing storage is not configured")
    return directory


@router.get("/current")
async def get_current_plan(
    request: Request,
    principal: Annotated[Principal, Security(require_scope("billing:read"))],
):
    plan = await _directory(request).get_plan(principal.tenant_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Voice billing plan is not configured")
    return plan


@router.put("/current")
async def configure_current_plan(
    body: ConfigureVoicePlanBody,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("billing:admin"))],
):
    if principal.auth_method != "oidc":
        raise HTTPException(
            status_code=403,
            detail="Billing plan changes require an OIDC administrator token",
        )
    account_store = getattr(request.app.state, "account_store", None)
    if account_store is None:
        raise HTTPException(status_code=503, detail="Account storage is not configured")
    billing_mode = await account_store.get_billing_mode(principal.tenant_id)
    if billing_mode is None:
        raise HTTPException(status_code=404, detail="Tenant account was not found")
    if billing_mode is BillingMode.PREPAID:
        if body.postpaid_credit_limit_minor is not None:
            raise HTTPException(
                status_code=422,
                detail="postpaid_credit_limit_minor is only valid for postpaid accounts",
            )
    elif body.prepaid_balance_minor is not None:
        raise HTTPException(
            status_code=422,
            detail="prepaid_balance_minor is only valid for prepaid accounts",
        )
    try:
        return await _directory(request).configure(
            tenant_id=principal.tenant_id,
            billing_mode=billing_mode,
            voice_enabled=body.voice_enabled,
            currency=body.currency,
            rate_per_minute_minor=body.rate_per_minute_minor,
            max_call_seconds=body.max_call_seconds,
            allowed_channels=body.allowed_channels,
            max_concurrent_sessions=body.max_concurrent_sessions,
            recording_allowed=body.recording_allowed,
            prepaid_balance_minor=body.prepaid_balance_minor,
            postpaid_credit_limit_minor=body.postpaid_credit_limit_minor,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
