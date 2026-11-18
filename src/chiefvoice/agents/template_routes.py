"""Platform-admin CRUD and API-key assignment routes for agent templates."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, Security, status
from pydantic import BaseModel, Field, model_validator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from chiefvoice.agents.post_call.output_schema import validate_output_schema
from chiefvoice.agents.template_store import SqlAgentTemplateDirectory
from chiefvoice.agents.templates import AgentTemplate, AgentTemplateBody, new_template_id
from chiefvoice.agents.post_call.queue import validate_public_webhook_url
from chiefvoice.core.config import settings
from chiefvoice.identity.auth import Principal, current_principal

router = APIRouter(prefix="/v1/admin/customers", tags=["agent templates"])


def _admin(required_scope: str):
    async def dependency(principal: Annotated[Principal, Security(current_principal)]):
        if principal.auth_method != "oidc" or principal.subject not in settings.platform_admin_subjects:
            raise HTTPException(status_code=403, detail="Platform administrator access is required")
        if required_scope not in principal.scopes:
            raise HTTPException(status_code=403, detail="Insufficient platform administrator scope")
        return principal
    return dependency


def _directory(request: Request) -> SqlAgentTemplateDirectory:
    directory = getattr(request.app.state, "agent_template_directory", None)
    if directory is None:
        raise HTTPException(status_code=503, detail="Agent template storage is not configured")
    return directory


def _validate_settings(body: AgentTemplateBody) -> dict:
    values = dict(body.settings)
    if body.kind == "post_call":
        time_zone = values.get("time_zone", "UTC")
        time_format = values.get("time_format", "%Y-%m-%dT%H:%M:%S%z")
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="Template time_zone must be a valid IANA zone") from exc
        if not isinstance(time_format, str) or "%z" not in time_format:
            raise HTTPException(status_code=422, detail="Template time_format must include %z")
        try:
            datetime.now().strftime(time_format)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail="Template time_format is invalid") from exc
        if "output_schema" in values:
            try:
                values["output_schema"] = validate_output_schema(values["output_schema"]).model_dump(exclude_none=True)
            except Exception as exc:
                raise HTTPException(status_code=422, detail="Template output_schema is invalid") from exc
        values["time_zone"] = time_zone
        values["time_format"] = time_format
    return values


async def _validate_failure_webhook(body: AgentTemplateBody) -> None:
    target_url = body.settings.get("failure_webhook_url")
    if target_url:
        try:
            await validate_public_webhook_url(target_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _require_api_webhook_for_fallback(request: Request, tenant_id: str, body: AgentTemplateBody) -> None:
    has_fallback = bool(
        body.settings.get("failure_webhook_url")
        or body.settings.get("failure_webhook_payload") is not None
    )
    if not has_fallback:
        return
    directory = getattr(request.app.state, "post_call_webhook_directory", None)
    if directory is None or await directory.get(tenant_id) is None:
        raise HTTPException(
            status_code=409,
            detail="Configure the API Call outcome webhook first; its signing secret authenticates template fallback deliveries.",
        )


def _serialize(template: AgentTemplate) -> dict:
    return {
        "id": template.template_id,
        "api_key_id": template.api_key_id,
        "kind": template.kind,
        "name": template.name,
        "instructions": template.instructions,
        "tools": list(template.tools),
        "variables": template.variables,
        "settings": template.settings,
        "revision": template.revision,
        "enabled": template.enabled,
        "is_default": template.is_default,
        "created_at": template.created_at.isoformat(),
        "updated_at": template.updated_at.isoformat(),
    }


def _to_template(
    template_id: str,
    body: AgentTemplateBody,
    revision: int = 1,
    api_key_id: str | None = None,
    is_default: bool = False,
) -> AgentTemplate:
    now = datetime.now(UTC)
    return AgentTemplate(
        template_id=template_id,
        kind=body.kind,
        name=body.name,
        instructions=body.instructions,
        tools=tuple(tool.model_dump() for tool in body.tools),
        variables=body.variables,
        settings=_validate_settings(body),
        revision=revision,
        enabled=True,
        created_at=now,
        updated_at=now,
        api_key_id=api_key_id,
        is_default=is_default,
    )


@router.get("/agent-templates")
async def list_agent_templates(
    request: Request,
    principal: Annotated[Principal, Security(_admin("platform:customers:read"))],
    kind: Literal["calling", "post_call"] | None = None,
):
    del principal
    return {"items": [_serialize(item) for item in await _directory(request).list_all(kind)]}


@router.post("/agent-templates", status_code=status.HTTP_201_CREATED)
async def create_agent_template(
    body: AgentTemplateBody,
    request: Request,
    principal: Annotated[Principal, Security(_admin("platform:customers:write"))],
):
    del principal
    await _validate_failure_webhook(body)
    if body.settings.get("failure_webhook_url") or body.settings.get("failure_webhook_payload") is not None:
        raise HTTPException(status_code=422, detail="Failure fallback settings must be scoped to an API template")
    try:
        created = await _directory(request).create(_to_template(new_template_id(), body))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _serialize(created)


@router.get("/agent-templates/{template_id}")
async def get_agent_template(
    template_id: str,
    request: Request,
    principal: Annotated[Principal, Security(_admin("platform:customers:read"))],
):
    del principal
    template = await _directory(request).get(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Agent template not found")
    return _serialize(template)


@router.put("/agent-templates/{template_id}")
async def update_agent_template(
    template_id: str,
    body: AgentTemplateBody,
    request: Request,
    principal: Annotated[Principal, Security(_admin("platform:customers:write"))],
):
    del principal
    directory = _directory(request)
    current = await directory.get(template_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Agent template not found")
    if current.kind != body.kind:
        raise HTTPException(status_code=422, detail="Template kind cannot be changed")
    await _validate_failure_webhook(body)
    if body.settings.get("failure_webhook_url") or body.settings.get("failure_webhook_payload") is not None:
        raise HTTPException(status_code=422, detail="Failure fallback settings must be scoped to an API template")
    try:
        updated = await directory.update(template_id, _to_template(template_id, body, current.revision + 1))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Agent template not found")
    return _serialize(updated)


@router.delete("/agent-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_template(
    template_id: str,
    request: Request,
    principal: Annotated[Principal, Security(_admin("platform:customers:write"))],
):
    del principal
    if not await _directory(request).archive(template_id):
        raise HTTPException(status_code=404, detail="Agent template not found")


class ApiTemplateAssignmentsBody(BaseModel):
    calling_template_ids: list[str] = Field(default_factory=list, max_length=100)
    post_call_template_ids: list[str] = Field(default_factory=list, max_length=100)
    default_calling_template_id: str | None = None
    default_post_call_template_id: str | None = None

    @model_validator(mode="after")
    def validate_defaults(self):
        for ids, default_id, label in (
            (self.calling_template_ids, self.default_calling_template_id, "calling"),
            (self.post_call_template_ids, self.default_post_call_template_id, "post-call"),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} template IDs must be unique")
            if default_id and default_id not in ids:
                raise ValueError(f"Default {label} template must be assigned")
        return self


@router.get("/api-keys/{key_id}/agent-templates")
async def get_api_template_assignments(
    key_id: str,
    request: Request,
    principal: Annotated[Principal, Security(_admin("platform:customers:read"))],
):
    del principal
    api_keys = getattr(request.app.state, "api_key_directory", None)
    key = next((item for item in await api_keys.list_all() if item.key_id == key_id), None) if api_keys else None
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    assignments = await _directory(request).list_api_assignments(key_id)
    return {
        "items": [_serialize(item) for item in assignments.get("items", [])],
        "calling": [_serialize(item) for item in assignments["calling"]],
        "post_call": [_serialize(item) for item in assignments["post_call"]],
        "default_calling_id": assignments["default_calling_id"],
        "default_post_call_id": assignments["default_post_call_id"],
    }


@router.post("/api-keys/{key_id}/agent-templates", status_code=status.HTTP_201_CREATED)
async def create_api_agent_template(
    key_id: str,
    body: AgentTemplateBody,
    request: Request,
    principal: Annotated[Principal, Security(_admin("platform:customers:write"))],
):
    del principal
    api_keys = getattr(request.app.state, "api_key_directory", None)
    key = next((item for item in await api_keys.list_all() if item.key_id == key_id), None) if api_keys else None
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    await _validate_failure_webhook(body)
    await _require_api_webhook_for_fallback(request, key.tenant_id, body)
    try:
        created = await _directory(request).create_for_api(
            key_id,
            _to_template(new_template_id(), body, api_key_id=key_id, is_default=body.is_default),
            is_default=body.is_default,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _serialize(created)


@router.get("/api-keys/{key_id}/agent-templates/{template_id}")
async def get_api_agent_template(
    key_id: str,
    template_id: str,
    request: Request,
    principal: Annotated[Principal, Security(_admin("platform:customers:read"))],
):
    del principal
    template = await _directory(request).get_for_api(key_id, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found for this API")
    return _serialize(template)


@router.put("/api-keys/{key_id}/agent-templates/{template_id}")
async def update_api_agent_template(
    key_id: str,
    template_id: str,
    body: AgentTemplateBody,
    request: Request,
    principal: Annotated[Principal, Security(_admin("platform:customers:write"))],
):
    del principal
    directory = _directory(request)
    current = await directory.get_for_api(key_id, template_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Template not found for this API")
    if current.kind != body.kind:
        raise HTTPException(status_code=422, detail="Template kind cannot be changed")
    await _validate_failure_webhook(body)
    api_keys = getattr(request.app.state, "api_key_directory", None)
    api_key = next((item for item in await api_keys.list_all() if item.key_id == key_id), None) if api_keys else None
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    await _require_api_webhook_for_fallback(request, api_key.tenant_id, body)
    try:
        updated = await directory.update_for_api(
            key_id,
            template_id,
            _to_template(template_id, body, current.revision + 1, api_key_id=key_id, is_default=current.is_default),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Template not found for this API")
    return _serialize(updated)


@router.delete("/api-keys/{key_id}/agent-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_agent_template(
    key_id: str,
    template_id: str,
    request: Request,
    principal: Annotated[Principal, Security(_admin("platform:customers:write"))],
):
    del principal
    if not await _directory(request).delete_for_api(key_id, template_id):
        raise HTTPException(status_code=404, detail="Template not found for this API")


@router.post("/api-keys/{key_id}/agent-templates/{template_id}/default")
async def set_default_api_agent_template(
    key_id: str,
    template_id: str,
    request: Request,
    principal: Annotated[Principal, Security(_admin("platform:customers:write"))],
):
    if not await _directory(request).set_default_for_api(key_id, template_id):
        raise HTTPException(status_code=404, detail="Template not found for this API")
    return await get_api_template_assignments(key_id, request, principal)


@router.put("/api-keys/{key_id}/agent-templates")
async def put_api_template_assignments(
    key_id: str,
    body: ApiTemplateAssignmentsBody,
    request: Request,
    principal: Annotated[Principal, Security(_admin("platform:customers:write"))],
):
    try:
        updated = await _directory(request).set_api_assignments(
            key_id,
            body.calling_template_ids,
            body.post_call_template_ids,
            body.default_calling_template_id,
            body.default_post_call_template_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Active API key not found")
    return await get_api_template_assignments(key_id, request, principal)
