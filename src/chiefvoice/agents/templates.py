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
            allowed_settings = {"time_zone", "time_format", "output_schema"}
            if unknown := self.settings.keys() - allowed_settings:
                raise ValueError("Unsupported post-call template settings: " + ", ".join(sorted(unknown)))
        elif self.settings.keys() - {"auto_hangup_when_complete"}:
            raise ValueError("Unsupported calling template settings")
        elif "auto_hangup_when_complete" in self.settings and not isinstance(
            self.settings["auto_hangup_when_complete"], bool
        ):
            raise ValueError("auto_hangup_when_complete must be a boolean")
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
