from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Security, status
from pydantic import BaseModel, Field

from chiefvoice.accounts.models import AccountStore, new_tenant_account
from chiefvoice.identity.auth import Principal, require_scope
from chiefvoice.billing import BillingMode

router = APIRouter(prefix="/v1/accounts", tags=["accounts"])


class CreateAccountBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    billing_mode: BillingMode


def _account_store(request: Request) -> AccountStore:
    store: AccountStore | None = getattr(request.app.state, "account_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Account storage is not configured")
    return store


def _serialize(account):
    return {
        "tenant_id": account.tenant_id,
        "display_name": account.display_name,
        "billing_mode": account.billing_mode.value,
        "created_at": account.created_at.isoformat(),
    }


async def _with_vertex_project(request: Request, account, payload: dict) -> dict:
    directory = getattr(request.app.state, "vertex_project_directory", None)
    if directory is not None:
        payload["vertex_project"] = await directory.request_provisioning(
            account.tenant_id, account.display_name
        )
    return payload


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_account(
    body: CreateAccountBody,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("accounts:create"))],
):
    store = _account_store(request)
    account = new_tenant_account(
        tenant_id=principal.tenant_id,
        display_name=body.display_name.strip(),
        billing_mode=body.billing_mode,
        created_by=principal.subject,
    )
    if not account.display_name:
        raise HTTPException(status_code=422, detail="Account name cannot be blank")
    if not await store.create(account):
        raise HTTPException(status_code=409, detail="An account already exists for this tenant")
    return await _with_vertex_project(request, account, _serialize(account))


@router.get("/current")
async def get_current_account(
    request: Request,
    principal: Annotated[Principal, Security(require_scope("accounts:read"))],
):
    account = await _account_store(request).get(principal.tenant_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account has not been created")
    return await _with_vertex_project(request, account, _serialize(account))


@router.get("/current/vertex-project")
async def get_current_vertex_project(
    request: Request,
    principal: Annotated[Principal, Security(require_scope("accounts:read"))],
):
    directory = getattr(request.app.state, "vertex_project_directory", None)
    if directory is None:
        raise HTTPException(status_code=404, detail="Tenant Vertex project provisioning is disabled")
    account = await _account_store(request).get(principal.tenant_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account has not been created")
    return await directory.request_provisioning(account.tenant_id, account.display_name)


@router.post("/current/vertex-project/retry")
async def retry_current_vertex_project(
    request: Request,
    principal: Annotated[Principal, Security(require_scope("accounts:create"))],
):
    directory = getattr(request.app.state, "vertex_project_directory", None)
    if directory is None:
        raise HTTPException(status_code=404, detail="Tenant Vertex project provisioning is disabled")
    try:
        return await directory.retry(principal.tenant_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Tenant Vertex project was not requested") from exc
