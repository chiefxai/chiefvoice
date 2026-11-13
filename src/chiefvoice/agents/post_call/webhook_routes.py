from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Security, status
from pydantic import BaseModel, Field

from chiefvoice.agents.post_call.queue import PostCallWebhookDirectory
from chiefvoice.identity.auth import Principal, require_scope

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


class ConfigurePostCallWebhookBody(BaseModel):
    target_url: str = Field(min_length=8, max_length=2048)


def _directory(request: Request) -> PostCallWebhookDirectory:
    directory: PostCallWebhookDirectory | None = getattr(
        request.app.state, "post_call_webhook_directory", None
    )
    if directory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Post-call webhook storage is not configured",
        )
    return directory


@router.put("/post-call")
async def configure_post_call_webhook(
    body: ConfigurePostCallWebhookBody,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("webhooks:write"))],
):
    try:
        secret, created_at = await _directory(request).configure(
            principal.tenant_id, body.target_url
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "status": "configured",
        "target_url": body.target_url,
        "signing_secret": secret,
        "configured_at": created_at.isoformat(),
        "message": "Copy the signing secret now; it will not be shown again.",
    }


@router.get("/post-call")
async def get_post_call_webhook(
    request: Request,
    principal: Annotated[Principal, Security(require_scope("webhooks:read"))],
):
    config = await _directory(request).get(principal.tenant_id)
    if config is None:
        raise HTTPException(status_code=404, detail="Post-call webhook is not configured")
    return {
        "status": "configured",
        "target_url": config["target_url"],
        "created_at": config["created_at"].isoformat(),
        "updated_at": config["updated_at"].isoformat(),
        "signing_secret_configured": True,
    }


@router.delete("/post-call", status_code=status.HTTP_204_NO_CONTENT)
async def delete_post_call_webhook(
    request: Request,
    principal: Annotated[Principal, Security(require_scope("webhooks:write"))],
):
    if not await _directory(request).delete(principal.tenant_id):
        raise HTTPException(status_code=404, detail="Post-call webhook is not configured")
