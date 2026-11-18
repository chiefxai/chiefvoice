"""Reusable, versioned agent prompt templates and safe context rendering."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TemplateKind = Literal["calling", "post_call"]
_PLACEHOLDER = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
_ALLOWED_TOOLS = {
    "calling": {"end_call"},
    "post_call": {"calculator", "time"},
}
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def validate_completion_requirements(value: Any) -> dict[str, Any]:
    """Validate the data contract required before a completed-call hangup."""
    if not isinstance(value, dict) or set(value) - {"always", "when"}:
        raise ValueError("completion_requirements must contain only always and when")
    always = value.get("always", [])
    branches = value.get("when", [])
    if not isinstance(always, list) or any(not isinstance(item, str) or not item for item in always):
        raise ValueError("completion_requirements.always must be an array of field names")
    if len(set(always)) != len(always):
        raise ValueError("completion_requirements.always must not contain duplicates")
    if not isinstance(branches, list) or len(branches) > 10:
        raise ValueError("completion_requirements.when must be an array with at most 10 branches")
    normalized = []
    for branch in branches:
        if not isinstance(branch, dict) or set(branch) != {"field", "equals", "required"}:
            raise ValueError("Each completion requirement branch needs field, equals, and required")
        field = branch["field"]
        required = branch["required"]
        if not isinstance(field, str) or not field:
            raise ValueError("Completion requirement branch field must be a non-empty string")
        if not isinstance(required, list) or any(not isinstance(item, str) or not item for item in required):
            raise ValueError("Completion requirement branch required must be an array of field names")
        if len(set(required)) != len(required):
            raise ValueError("Completion requirement branch fields must not contain duplicates")
        if not isinstance(branch["equals"], (str, int, float, bool, type(None))):
            raise ValueError("Completion requirement branch equals must be a JSON scalar")
        normalized.append({"field": field, "equals": branch["equals"], "required": required})
    if not always and not normalized:
        raise ValueError("completion_requirements must require at least one field")
    return {"always": always, "when": normalized}


class BuiltinTool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["builtin"] = "builtin"
    name: str = Field(min_length=1, max_length=80)


class AgentTemplateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    kind: TemplateKind
    instructions: str = Field(min_length=1, max_length=6000)
    tools: list[BuiltinTool] = Field(default_factory=list, max_length=10)
    variables: dict[str, Any] = Field(default_factory=dict, max_length=50)
    settings: dict[str, Any] = Field(default_factory=dict, max_length=30)
    is_default: bool = False

    @field_validator("name", "instructions")
    @classmethod
    def trim_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be blank")
        return value

    @field_validator("tools")
    @classmethod
    def validate_tools(cls, tools: list[BuiltinTool], info):
        kind = info.data.get("kind")
        allowed = _ALLOWED_TOOLS.get(kind, set())
        names = [tool.name for tool in tools]
        if len(set(names)) != len(names):
            raise ValueError("Template tools must be unique")
        if any(name not in allowed for name in names):
            raise ValueError(f"Unsupported {kind or 'agent'} template tool")
        return tools

    @model_validator(mode="after")
    def validate_template(self):
        names = set(_PLACEHOLDER.findall(self.instructions))
        if undeclared := names - self.variables.keys():
            raise ValueError("Declare every instruction placeholder in variables: " + ", ".join(sorted(undeclared)))
        for name, spec in self.variables.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"Invalid template variable name: {name}")
            if (
                not isinstance(spec, dict)
                or spec.get("type", "string") not in {"string", "integer", "number", "boolean", "object", "array"}
                or not isinstance(spec.get("required", False), bool)
            ):
                raise ValueError(f"Invalid variable declaration: {name}")
            if set(spec) - {"type", "required", "description"}:
                raise ValueError(f"Unsupported variable declaration fields: {name}")
        if self.kind == "post_call":
            allowed_settings = {
                "time_zone", "time_format", "output_schema",
                "failure_webhook_url", "failure_webhook_payload",
            }
            if unknown := self.settings.keys() - allowed_settings:
                raise ValueError("Unsupported post-call template settings: " + ", ".join(sorted(unknown)))
            failure_url = self.settings.get("failure_webhook_url")
            if failure_url is not None and (
                not isinstance(failure_url, str) or not failure_url.strip() or len(failure_url) > 2048
            ):
                raise ValueError("failure_webhook_url must be a non-empty URL up to 2048 characters")
            failure_payload = self.settings.get("failure_webhook_payload")
            if failure_payload is not None:
                if not isinstance(failure_payload, dict):
                    raise ValueError("failure_webhook_payload must be a JSON object")
                if len(json.dumps(failure_payload, ensure_ascii=False).encode("utf-8")) > 16 * 1024:
                    raise ValueError("failure_webhook_payload exceeds the 16 KiB limit")
        else:
            allowed_settings = {
                "auto_hangup_when_complete",
                "language",
                "completion_requirements",
            }
            if unknown := self.settings.keys() - allowed_settings:
                raise ValueError("Unsupported calling template settings: " + ", ".join(sorted(unknown)))
            if "auto_hangup_when_complete" in self.settings and not isinstance(
                self.settings["auto_hangup_when_complete"], bool
            ):
                raise ValueError("auto_hangup_when_complete must be a boolean")
            language = self.settings.get("language")
            if language is not None and (
                not isinstance(language, str) or not _LANGUAGE_TAG.fullmatch(language)
            ):
                raise ValueError("Calling template language must be a valid BCP 47 language tag")
            if "completion_requirements" in self.settings:
                self.settings["completion_requirements"] = validate_completion_requirements(
                    self.settings["completion_requirements"]
                )
        return self


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    template_id: str
    kind: TemplateKind
    name: str
    instructions: str
    tools: tuple[dict[str, str], ...]
    variables: dict[str, Any]
    settings: dict[str, Any]
    revision: int
    enabled: bool
    created_at: datetime
    updated_at: datetime
    api_key_id: str | None = None
    is_default: bool = False


def render_template(template: AgentTemplate, context: dict[str, Any]) -> str:
    """Render declared simple-name placeholders without evaluating user code."""
    declared = template.variables
    for key, spec in declared.items():
        required = spec.get("required", False)
        if required and key not in context:
            raise ValueError(f"Missing required template variable: {key}")
        if key in context:
            expected = spec.get("type", "string")
            value = context[key]
            valid = {
                "string": lambda v: isinstance(v, str),
                "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
                "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
                "boolean": lambda v: isinstance(v, bool),
                "object": lambda v: isinstance(v, dict),
                "array": lambda v: isinstance(v, list),
            }[expected](value)
            if not valid:
                raise ValueError(f"Invalid type for template variable: {key}")

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            raise ValueError(f"Missing template variable: {key}")
        value = context[key]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    rendered = _PLACEHOLDER.sub(replace, template.instructions)
    if len(rendered.encode("utf-8")) > 12_000:
        raise ValueError("Rendered template instructions exceed 12000 bytes")
    return rendered


def new_template_id() -> str:
    return str(uuid4())


def now_utc() -> datetime:
    return datetime.now(UTC)
