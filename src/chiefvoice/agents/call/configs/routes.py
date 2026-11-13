import re
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Security, status
from pydantic import BaseModel, Field, field_validator
from pipecat.transcriptions.language import Language
from sqlalchemy.exc import IntegrityError

from chiefvoice.agents.call.configs.profiles import (
    AgentProfile,
    AgentProfileDirectory,
    SpeakingStyle,
)
from chiefvoice.identity.auth import Principal, require_scope

router = APIRouter(prefix="/v1/agents", tags=["voice agents"])
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class AgentProfileBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    language: str = Field(default="en-US", min_length=2, max_length=35)
    voice: str = Field(default="Charon", min_length=1, max_length=80)
    speaking_style: SpeakingStyle = SpeakingStyle.FRIENDLY
    instructions: str = Field(default="", max_length=6000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Agent name cannot be blank")
        return value

    @field_validator("language")
    @classmethod
    def validate_language_tag(cls, value: str) -> str:
        value = value.strip()
        if not _LANGUAGE_TAG.fullmatch(value):
            raise ValueError("Language must be a BCP 47 language tag, such as en-US or hi-IN")
        try:
            Language(value)
        except ValueError as exc:
            raise ValueError("Language is not supported by the configured realtime provider") from exc
        return value

    @field_validator("voice")
    @classmethod
    def normalize_voice(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Voice cannot be blank")
        return value


def _directory(request: Request) -> AgentProfileDirectory:
    directory: AgentProfileDirectory | None = getattr(request.app.state, "agent_profile_directory", None)
    if directory is None:
        raise HTTPException(status_code=503, detail="Agent profile storage is not configured")
    return directory


def _serialize(profile: AgentProfile) -> dict:
    return {
        "id": profile.agent_id,
        "name": profile.name,
        "language": profile.language,
        "voice": profile.voice,
        "speaking_style": profile.speaking_style.value,
        "instructions": profile.instructions,
        "enabled": profile.enabled,
        "created_at": profile.created_at.isoformat(),
        "updated_at": profile.updated_at.isoformat(),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent_profile(
    body: AgentProfileBody,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("agents:write"))],
):
    profile = AgentProfile(
        agent_id=str(uuid4()),
        tenant_id=principal.tenant_id,
        name=body.name,
        language=body.language,
        voice=body.voice,
        speaking_style=body.speaking_style,
        instructions=body.instructions,
        enabled=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    try:
        saved = await _directory(request).create(profile)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="An agent with this name already exists") from exc
    return _serialize(saved)


@router.get("")
async def list_agent_profiles(
    request: Request,
    principal: Annotated[Principal, Security(require_scope("agents:read"))],
):
    profiles = await _directory(request).list_for_tenant(principal.tenant_id)
    return {"items": [_serialize(profile) for profile in profiles]}


@router.get("/{agent_id}")
async def get_agent_profile(
    agent_id: str,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("agents:read"))],
):
    profile = await _directory(request).get(principal.tenant_id, agent_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    return _serialize(profile)


@router.put("/{agent_id}")
async def update_agent_profile(
    agent_id: str,
    body: AgentProfileBody,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("agents:write"))],
):
    profile = AgentProfile(
        agent_id=agent_id,
        tenant_id=principal.tenant_id,
        name=body.name,
        language=body.language,
        voice=body.voice,
        speaking_style=body.speaking_style,
        instructions=body.instructions,
        enabled=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    try:
        saved = await _directory(request).update(profile)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="An agent with this name already exists") from exc
    if saved is None:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    return _serialize(saved)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_agent_profile(
    agent_id: str,
    request: Request,
    principal: Annotated[Principal, Security(require_scope("agents:write"))],
):
    if not await _directory(request).archive(principal.tenant_id, agent_id):
        raise HTTPException(status_code=404, detail="Agent profile not found")
