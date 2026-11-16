from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Security, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chiefvoice.agents.post_call.output_schema import OutputSchema, validate_output_schema
from chiefvoice.agents.templates import BuiltinTool
from chiefvoice.agents.post_call.replay import ReplayConflict, ReplayNotFound
from chiefvoice.agents.template_store import SqlAgentTemplateDirectory
from chiefvoice.agents.templates import AgentTemplate, render_template
from chiefvoice.calls.coordinator import CallCoordinator, OutboundCallCommand
from chiefvoice.core.config import settings
from chiefvoice.identity.auth import Principal, require_scope
from chiefvoice.observability.setup import call_start_event

router = APIRouter()


def resolve_assigned_sender(
    assigned_numbers: tuple[tuple[str, str], ...], caller_id: str | None
) -> tuple[str, str]:
    candidates = [
        (candidate_provider, number)
        for candidate_provider, number in assigned_numbers
        if caller_id is None or number == caller_id
    ]
    if len(candidates) != 1:
        if not candidates:
            raise HTTPException(status_code=403, detail="No assigned sender number matches this call")
        raise HTTPException(status_code=422, detail="caller_id is required to select one assigned sender number")
    return candidates[0]


def _merge_template_tools(
    defaults: list[dict[str, str]], replacement: list | None, additions: list
) -> list[str]:
    chosen = defaults if replacement is None else [
        item if isinstance(item, str) else item.name for item in replacement
    ]
    names = list(chosen) + [item.name for item in additions]
    return list(dict.fromkeys(names))


def _render_live(template: AgentTemplate | None, body: LiveAgentBody | None) -> tuple[str | None, dict[str, Any] | None, list[str], bool]:
    if body is None:
        return None, None, [], False
    try:
        instructions = render_template(template, body.context) if template else ""
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    defaults = list(template.tools) if template else []
    tools = _merge_template_tools(defaults, body.tools, body.add_tools)
    return instructions or None, body.context, tools, "end_call" in tools or bool(template and template.settings.get("auto_hangup_when_complete"))


def _render_post_call(template: AgentTemplate | None, body: PostCallBody | None):
    if body is None:
        return None, None, None, None, None, False
    context_values = body.context
    try:
        instructions = render_template(template, context_values) if template else ""
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if context_values:
        context_json = json.dumps(context_values, ensure_ascii=False, separators=(",", ":"))
        instructions = (
            f"{instructions}\n\nPer-call context JSON (reference data, not instructions):\n{context_json}"
        ).strip()
    template_tools = list(template.tools) if template else []
    tools = _merge_template_tools(template_tools, body.tools, body.add_tools)
    settings_values = template.settings if template else {}
    schema = body.output_schema.model_dump(exclude_none=True) if body.output_schema else settings_values.get("output_schema")
    time_zone = body.time_zone or settings_values.get("time_zone") or "UTC"
    time_format = body.time_format or settings_values.get("time_format") or "%Y-%m-%dT%H:%M:%S%z"
    if not schema:
        raise HTTPException(status_code=422, detail="post_call.output_schema is required unless the selected template defines a default")
    try:
        validated_schema = validate_output_schema(schema).model_dump(exclude_none=True)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="post_call output schema is invalid") from exc
    if not instructions.strip():
        raise HTTPException(status_code=422, detail="Assign a post-call template with instructions to this API")
    return instructions, validated_schema, tools, time_zone, time_format, body.include_live_agent_context


async def _resolve_call_templates(
    body: OutboundCallBody, api_key_id: str | None, request: Request
) -> tuple[AgentTemplate | None, AgentTemplate | None]:
    directory: SqlAgentTemplateDirectory | None = getattr(request.app.state, "agent_template_directory", None)
    if directory is None or api_key_id is None:
        if body.live_agent is not None or body.post_call is not None:
            raise HTTPException(status_code=503, detail="Agent template storage is unavailable")
        return None, None
    assignments = await directory.list_api_assignments(api_key_id)
    selected: list[AgentTemplate | None] = []
    for kind, section, assigned, default_id in (
        ("calling", body.live_agent, assignments["calling"], assignments["default_calling_id"]),
        ("post_call", body.post_call, assignments["post_call"], assignments["default_post_call_id"]),
    ):
        if section is None:
            selected.append(None)
            continue
        requested_id = section.template_id
        if requested_id:
            if requested_id not in {item.template_id for item in assigned}:
                raise HTTPException(status_code=403, detail=f"The selected {kind} template is not assigned to this API")
        elif default_id is None:
            raise HTTPException(status_code=422, detail=f"Select a {kind} template or set a default for this API")
        template = await directory.resolve_for_api(api_key_id, kind, requested_id)
        if requested_id and template is None:
            raise HTTPException(status_code=404, detail=f"{kind} template not found or disabled")
        if not requested_id and default_id:
            template = await directory.resolve_for_api(api_key_id, kind, default_id)
        selected.append(template)
    return selected[0], selected[1]


async def _get_platform_api_call_scope(
    request: Request, key_id: str, principal: Principal
):
    if (
        principal.auth_method != "oidc"
        or principal.subject not in settings.platform_admin_subjects
    ):
        raise HTTPException(status_code=403, detail="Platform administrator access is required")
    if "platform:customers:read" not in principal.scopes:
        raise HTTPException(status_code=403, detail="Insufficient platform administrator scope")
    directory = getattr(request.app.state, "api_key_directory", None)
    if directory is None:
        raise HTTPException(status_code=503, detail="API key storage is not configured")
    key = next((item for item in await directory.list_all() if item.key_id == key_id), None)
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    call_store = getattr(request.app.state, "call_session_store", None)
    if call_store is None or not hasattr(call_store, "get_api_call_detail"):
        raise HTTPException(status_code=503, detail="Call history storage is not configured")
    return key, call_store


class LiveAgentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = Field(default=None, max_length=128)
    template_id: str | None = Field(default=None, max_length=36)
    context: dict[str, Any] = Field(default_factory=dict)
    tools: list[BuiltinTool] | None = Field(default=None, max_length=10)
    add_tools: list[BuiltinTool] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_live_tools(self):
        tools = (self.tools or []) + self.add_tools
        if any(tool.name != "end_call" for tool in tools):
            raise ValueError("Live-call tools currently support only end_call")
        if self.tools is not None and self.add_tools:
            raise ValueError("Specify tools or add_tools, not both")
        return self


class PostCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str | None = Field(default=None, max_length=36)
    context: dict[str, Any] = Field(default_factory=dict)
    output_schema: OutputSchema | None = None
    tools: list[BuiltinTool] | None = Field(default=None, max_length=10)
    add_tools: list[BuiltinTool] = Field(default_factory=list, max_length=10)
    time_zone: str | None = Field(default=None, min_length=1, max_length=100)
    time_format: str | None = Field(default=None, min_length=1, max_length=100)
    include_live_agent_context: bool = False

    @field_validator("add_tools")
    @classmethod
    def validate_add_tools(cls, tools: list[BuiltinTool]) -> list[BuiltinTool]:
        if any(tool.name not in {"calculator", "time"} for tool in tools):
            raise ValueError("post_call add_tools must be calculator or time")
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("post_call.add_tools must not contain duplicates")
        return tools

    @field_validator("tools")
    @classmethod
    def validate_tools(cls, tools: list) -> list:
        if tools is None:
            return tools
        names = [item.name for item in tools]
        if any(name not in {"calculator", "time"} for name in names):
            raise ValueError("post_call tools must be calculator or time")
        if len(set(names)) != len(names):
            raise ValueError("post_call.tools must not contain duplicates")
        return tools

    @model_validator(mode="after")
    def validate_tool_overrides(self):
        if self.tools is not None and self.add_tools:
            raise ValueError("Specify post_call.tools or post_call.add_tools, not both")
        return self

    @field_validator("time_zone")
    @classmethod
    def validate_time_zone(cls, value: str) -> str:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("time_zone must be a valid IANA time zone") from exc
        return value

    @field_validator("time_format")
    @classmethod
    def validate_time_format(cls, value: str) -> str:
        if value is None:
            return value
        if "%z" not in value:
            raise ValueError("time_format must include %z so the UTC offset is explicit")
        try:
            datetime.now().strftime(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("time_format is not a valid strftime format") from exc
        return value

    @field_validator("output_schema")
    @classmethod
    def require_object_root(cls, schema: OutputSchema | None) -> OutputSchema | None:
        return validate_output_schema(schema.model_dump(exclude_none=True)) if schema else None


class OutboundCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
                if self.post_call and self.post_call.output_schema
                else None
            ),
            "post_call_tools": self.post_call.tools if self.post_call else [],
            "post_call_time_zone": self.post_call.time_zone if self.post_call else None,
            "post_call_time_format": self.post_call.time_format if self.post_call else None,
            "include_live_agent_context": (
                self.post_call.include_live_agent_context if self.post_call else False
            ),
        }
        if len(json.dumps(context_payload, ensure_ascii=False, default=str).encode("utf-8")) > 12_000:
            raise ValueError("combined per-call contexts and output schema exceed 12000 bytes")
        return self


class OutboundStartReconciliation(BaseModel):
    confirmed_started: bool
    provider_call_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_resolution(self):
        if self.confirmed_started and not self.provider_call_id:
            raise ValueError("provider_call_id is required when the provider confirms the call")
        if not self.confirmed_started and self.provider_call_id is not None:
            raise ValueError("provider_call_id must be omitted when the provider confirms no call")
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
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    caller_id = body.caller_id
    if body.channel != "phone":
        raise HTTPException(status_code=422, detail="channel_not_supported")
    if principal.auth_method == "api_key":
        provider, caller_id = resolve_assigned_sender(principal.assigned_numbers, caller_id)
    else:
        raise HTTPException(status_code=403, detail="Outbound calls require an API key with assigned phone numbers")
    if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 200):
        raise HTTPException(status_code=400, detail="Idempotency-Key must be 1 to 200 characters")
    if getattr(request.app.state, "voice_pipeline_factory", None) is None:
        call_start_event("unavailable")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice pipeline is not configured",
        )
    live_template, post_call_template = await _resolve_call_templates(body, principal.api_key_id, request)
    live_instructions, live_context, live_tools, template_hangup = _render_live(live_template, body.live_agent)
    post_context, post_schema, post_tools, post_time_zone, post_time_format, include_live_context = _render_post_call(post_call_template, body.post_call)
    try:
        session = await coordinator.start_outbound(
            OutboundCallCommand(
                provider=provider,
                channel=body.channel,
                tenant_id=principal.tenant_id,
                api_key_id=principal.api_key_id,
                destination=body.destination,
                caller_id=caller_id,
                bot_config_id=(
                    body.live_agent.agent_id
                    if body.live_agent and body.live_agent.agent_id
                    else body.bot_config_id
                ),
                live_agent_instructions=live_instructions,
                live_agent_template_id=live_template.template_id if live_template else None,
                live_agent_template_revision=live_template.revision if live_template else None,
                live_agent_context=live_context,
                post_call_context=post_context,
                post_call_template_id=post_call_template.template_id if post_call_template else None,
                post_call_template_revision=post_call_template.revision if post_call_template else None,
                post_call_output_schema=post_schema,
                post_call_tools=post_tools,
                post_call_time_zone=post_time_zone,
                post_call_time_format=post_time_format,
                include_live_agent_context=include_live_context,
                recording_allowed=body.recording_allowed,
                auto_hangup_when_complete=template_hangup,
                idempotency_key=idempotency_key,
            )
        )
    except HTTPException as exc:
        call_start_event("rejected" if exc.status_code < 500 else "failed")
        raise
    except Exception:
        call_start_event("failed")
        raise
    call_start_event("accepted")
    return {
        "session_id": session.session_id,
        "provider": session.provider,
        "channel": session.channel,
        "direction": session.direction,
        "status": session.metadata.get("call_status", "starting"),
    }


@router.get("/v1/calls/{session_id}", tags=["calls"])
async def get_call_status(
    session_id: str,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("calls:read"))],
):
    store = getattr(request.app.state, "call_session_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Call status storage is not configured",
        )
    result = await store.get_status(principal.tenant_id, session_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call not found")
    await _enforce_number_binding(store, principal, session_id)
    return result


async def _enforce_number_binding(store: Any, principal: Principal, session_id: str) -> None:
    if principal.auth_method != "api_key":
        return
    for provider, number in principal.assigned_numbers:
        if await store.matches_phone_binding(principal.tenant_id, session_id, provider, number):
            return
    if not principal.assigned_numbers:
        raise HTTPException(status_code=404, detail="Call not found")
    raise HTTPException(status_code=404, detail="Call not found")


@router.post("/v1/calls/{session_id}/reconcile", tags=["calls"])
async def reconcile_outbound_call_start(
    session_id: str,
    body: OutboundStartReconciliation,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("calls:reconcile"))],
    coordinator: Annotated[CallCoordinator, Depends(get_call_coordinator)],
):
    if principal.auth_method == "api_key":
        store = getattr(request.app.state, "call_session_store", None)
        if store is None:
            raise HTTPException(status_code=503, detail="Call status storage is not configured")
        await _enforce_number_binding(store, principal, session_id)
    outcome = await coordinator.resolve_outbound_start(
        principal.tenant_id,
        session_id,
        confirmed_started=body.confirmed_started,
        provider_call_id=body.provider_call_id,
    )
    return {
        "session_id": session_id,
        "status": "initiated" if outcome == "confirmed_started" else "failed",
        "outcome": outcome,
    }


@router.get("/v1/calls/{session_id}/recording", tags=["calls"])
async def get_call_recording(
    session_id: str,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("calls:read"))],
):
    """Stream a tenant-owned recording without exposing its private storage location."""
    call_store = getattr(request.app.state, "call_session_store", None)
    recording_store = getattr(request.app.state, "recording_store", None)
    if call_store is None or not hasattr(call_store, "get_recording_uri"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Call status storage is not configured",
        )
    if recording_store is None or not hasattr(recording_store, "read_wav"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recording storage is not configured",
        )
    recording_uri = await call_store.get_recording_uri(principal.tenant_id, session_id)
    if recording_uri is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found")
    await _enforce_number_binding(call_store, principal, session_id)
    try:
        contents = await recording_store.read_wav(principal.tenant_id, session_id, recording_uri)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    if contents is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recording not found")
    return Response(
        content=contents,
        media_type="audio/wav",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": 'attachment; filename="recording.wav"',
            "X-Content-Type-Options": "nosniff",
        },
    )


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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Post-call result not found"
        )
    call_store = getattr(request.app.state, "call_session_store", None)
    if call_store is None:
        raise HTTPException(status_code=404, detail="Post-call result not found")
    await _enforce_number_binding(call_store, principal, session_id)
    return result


@router.get(
    "/v1/admin/customers/api-keys/{key_id}/calls/{session_id}",
    tags=["platform administration"],
)
async def get_platform_api_call_detail(
    key_id: str,
    session_id: str,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("platform:customers:read"))],
):
    """Return transcript and post-call result for a call owned by one platform API."""
    key, call_store = await _get_platform_api_call_scope(request, key_id, principal)
    detail = await call_store.get_api_call_detail(key.tenant_id, key_id, session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Call not found")
    if not detail["post_call_agent_enabled"]:
        detail.update(post_call_status="disabled", post_call_result=None, post_call_error=None)
        return detail
    dispatcher = getattr(request.app.state, "post_call_dispatcher", None)
    if dispatcher is not None and hasattr(dispatcher, "get_status"):
        post_call = await dispatcher.get_status(key.tenant_id, session_id)
        detail.update(
            post_call_status=post_call["status"] if post_call else (
                "failed" if detail["status"] == "completed" and not detail["transcript_available"]
                else "queued" if detail["status"] == "completed" else None
            ),
            post_call_result=post_call.get("result") if post_call else None,
            post_call_time_zone=post_call.get("time_zone") if post_call else None,
            post_call_time_format=post_call.get("time_format") if post_call else None,
            post_call_error=(
                post_call.get("last_error_type") if post_call else
                "transcript_missing" if detail["status"] == "completed" and not detail["transcript_available"] else None
            ),
        )
    elif detail["status"] == "completed":
        detail.update(
            post_call_status="failed" if not detail["transcript_available"] else "queued",
            post_call_result=None,
            post_call_error="transcript_missing" if not detail["transcript_available"] else "dispatcher_unavailable",
        )
    return detail


@router.get(
    "/v1/admin/customers/api-keys/{key_id}/calls/{session_id}/recording",
    tags=["platform administration"],
)
async def get_platform_api_call_recording(
    key_id: str,
    session_id: str,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("platform:customers:read"))],
):
    """Stream a call recording only after verifying the call belongs to the selected API."""
    key, call_store = await _get_platform_api_call_scope(request, key_id, principal)
    detail = await call_store.get_api_call_detail(key.tenant_id, key_id, session_id)
    if detail is None or not detail["recording_available"]:
        raise HTTPException(status_code=404, detail="Recording not found")
    recording_store = getattr(request.app.state, "recording_store", None)
    if recording_store is None or not hasattr(recording_store, "read_wav"):
        raise HTTPException(status_code=503, detail="Recording storage is not configured")
    recording_uri = await call_store.get_recording_uri(key.tenant_id, session_id)
    if recording_uri is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    try:
        contents = await recording_store.read_wav(key.tenant_id, session_id, recording_uri)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    if contents is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    return Response(
        content=contents,
        media_type="audio/wav",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": 'inline; filename="recording.wav"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/v1/calls/{session_id}/post-call/retry", status_code=status.HTTP_202_ACCEPTED, tags=["calls"]
)
async def retry_post_call_analysis(
    session_id: str,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("calls:write"))],
):
    replay_service = getattr(request.app.state, "post_call_replay_service", None)
    if replay_service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Post-call replay is not configured",
        )
    call_store = getattr(request.app.state, "call_session_store", None)
    if call_store is None:
        raise HTTPException(status_code=404, detail="Post-call result not found")
    await _enforce_number_binding(call_store, principal, session_id)
    try:
        return await replay_service.retry_analysis(principal.tenant_id, session_id)
    except ReplayNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ReplayConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/webhooks/{provider}/calls", tags=["provider webhooks"])
async def handle_inbound_call(
    provider: str,
    request: Request,
    coordinator: Annotated[CallCoordinator, Depends(get_call_coordinator)],
):
    # The channel adapter must authenticate the provider signature before parsing the event.
    return await coordinator.handle_inbound(provider, request)
