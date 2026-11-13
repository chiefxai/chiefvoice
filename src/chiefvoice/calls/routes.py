import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chiefvoice.agents.post_call.output_schema import OutputSchema, validate_output_schema
from chiefvoice.calls.coordinator import CallCoordinator, OutboundCallCommand
from chiefvoice.identity.auth import Principal, require_scope

router = APIRouter()


class LiveAgentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = Field(default=None, max_length=128)
    context: dict[str, Any] = Field(default_factory=dict)


class PostCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str = Field(min_length=1, max_length=8_000)
    output_schema: OutputSchema

    @field_validator("output_schema")
    @classmethod
    def require_object_root(cls, schema: OutputSchema) -> OutputSchema:
        return validate_output_schema(schema.model_dump(exclude_none=True))


class OutboundCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=40)
    channel: str = Field(min_length=1, max_length=40)
    destination: str = Field(min_length=3, max_length=64)
    caller_id: str | None = Field(default=None, max_length=64)
    live_agent: LiveAgentBody | None = None
    post_call: PostCallBody | None = None
    # Kept temporarily for existing clients; new integrations should use live_agent.agent_id.
    bot_config_id: str | None = Field(default=None, max_length=128)
    recording_allowed: bool = False

    @model_validator(mode="after")
    def validate_context_payloads(self) -> "OutboundCallBody":
        if self.live_agent and self.live_agent.agent_id and self.bot_config_id:
            if self.live_agent.agent_id != self.bot_config_id:
                raise ValueError("live_agent.agent_id and bot_config_id must match")
        live_context: dict[str, Any] = self.live_agent.context if self.live_agent else {}
        context_payload = {
            "live_agent_context": live_context,
            "post_call_context": self.post_call.context if self.post_call else None,
            "post_call_output_schema": (
                self.post_call.output_schema.model_dump(exclude_none=True)
                if self.post_call
                else None
            ),
        }
        if len(json.dumps(context_payload, ensure_ascii=False).encode("utf-8")) > 6_000:
            raise ValueError("combined per-call contexts and output schema exceed 6000 bytes")
        return self


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
            bot_config_id=(
                body.live_agent.agent_id
                if body.live_agent and body.live_agent.agent_id
                else body.bot_config_id
            ),
            live_agent_context=body.live_agent.context if body.live_agent else None,
            post_call_context=body.post_call.context if body.post_call else None,
            post_call_output_schema=(
                body.post_call.output_schema.model_dump(exclude_none=True)
                if body.post_call
                else None
            ),
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
