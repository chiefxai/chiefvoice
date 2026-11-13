from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from pydantic import BaseModel, Field

from chiefvoice.identity.auth import Principal, require_scope
from chiefvoice.calls.coordinator import CallCoordinator, OutboundCallCommand

router = APIRouter()


class OutboundCallBody(BaseModel):
    provider: str = Field(min_length=1, max_length=40)
    channel: str = Field(min_length=1, max_length=40)
    destination: str = Field(min_length=3, max_length=64)
    caller_id: str | None = Field(default=None, max_length=64)
    bot_config_id: str | None = Field(default=None, max_length=128)
    recording_allowed: bool = False


def get_call_coordinator(request: Request) -> CallCoordinator:
    coordinator: CallCoordinator | None = getattr(request.app.state, "call_coordinator", None)
    if coordinator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Call services are not configured",
        )
    return coordinator


@router.post("/v1/calls/outbound", status_code=status.HTTP_202_ACCEPTED, tags=["calls"])
async def start_outbound_call(
    body: OutboundCallBody,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("calls:write"))],
    coordinator: Annotated[CallCoordinator, Depends(get_call_coordinator)],
):
    if getattr(request.app.state, "voice_pipeline_factory", None) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice pipeline is not configured",
        )
    session = await coordinator.start_outbound(
        OutboundCallCommand(
            provider=body.provider,
            channel=body.channel,
            tenant_id=principal.tenant_id,
            destination=body.destination,
            caller_id=body.caller_id,
            bot_config_id=body.bot_config_id,
            recording_allowed=body.recording_allowed,
        )
    )
    return {
        "session_id": session.session_id,
        "provider": session.provider,
        "channel": session.channel,
        "direction": session.direction,
        "status": "starting",
    }


@router.get("/v1/calls/{session_id}/post-call", tags=["calls"])
async def get_post_call_result(
    session_id: str,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("calls:read"))],
):
    dispatcher = getattr(request.app.state, "post_call_dispatcher", None)
    if dispatcher is None or not hasattr(dispatcher, "get_status"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Post-call result storage is not configured",
        )
    result = await dispatcher.get_status(principal.tenant_id, session_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post-call result not found")
    return result


@router.post("/webhooks/{provider}/calls", tags=["provider webhooks"])
async def handle_inbound_call(
    provider: str,
    request: Request,
    coordinator: Annotated[CallCoordinator, Depends(get_call_coordinator)],
):
    # The channel adapter must authenticate the provider signature before parsing the event.
    return await coordinator.handle_inbound(provider, request)
