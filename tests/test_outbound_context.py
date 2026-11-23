from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from chiefvoice.agents.post_call.output_schema import build_output_model, validate_output_schema
from chiefvoice.calls.routes import OutboundCallBody, _merge_template_tools, _render_live


def _schema():
    return {
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": ["confirmed", "reschedule_requested", "unresolved"],
            },
            "notes": {"type": "string"},
        },
        "required": ["outcome", "notes"],
        "additionalProperties": False,
    }


def test_call_contract_keeps_live_and_post_call_context_separate():
    body = OutboundCallBody.model_validate(
        {
            "channel": "phone",
            "destination": "+15551234567",
            "live_agent": {
                "agent_id": "agent-a",
                "context": {"patient_name": "Alex"},
            },
            "post_call": {
                "context": {"task_note": "confirmed appointment outcome"},
                "output_schema": _schema(),
                "tools": [
                    {"type": "builtin", "name": "calculator"},
                    {"type": "builtin", "name": "time"},
                ],
                "time_zone": "Asia/Kolkata",
                "time_format": "%Y-%m-%dT%H:%M:%S%z",
                "include_live_agent_context": True,
            },
        }
    )

    assert body.live_agent.context == {"patient_name": "Alex"}
    assert body.post_call.context == {"task_note": "confirmed appointment outcome"}
    assert body.post_call.output_schema.type == "object"
    assert [tool.name for tool in body.post_call.tools] == ["calculator", "time"]
    assert body.post_call.time_zone == "Asia/Kolkata"
    assert body.post_call.include_live_agent_context is True


def test_time_tool_requires_valid_zone_and_explicit_offset_format():
    request = {
        "channel": "phone",
        "destination": "+15551234567",
        "post_call": {
            "context": {},
            "output_schema": _schema(),
            "tools": [{"type": "builtin", "name": "time"}],
        },
    }
    with pytest.raises(ValidationError):
        OutboundCallBody.model_validate(
            {**request, "post_call": {**request["post_call"], "time_zone": "Mars/Olympus"}}
        )
    with pytest.raises(ValidationError):
        OutboundCallBody.model_validate(
            {
                **request,
                "post_call": {**request["post_call"], "time_format": "%Y-%m-%d %H:%M"},
            }
        )


def test_output_model_rejects_wrong_enum_missing_field_and_extra_field():
    output_model = build_output_model(validate_output_schema(_schema()))

    with pytest.raises(ValidationError):
        output_model.model_validate({"outcome": "maybe", "notes": "not supported"})
    with pytest.raises(ValidationError):
        output_model.model_validate({"outcome": "confirmed"})
    with pytest.raises(ValidationError):
        output_model.model_validate(
            {"outcome": "confirmed", "notes": "confirmed", "unexpected": True}
        )


def test_custom_output_model_returns_original_schema_property_names():
    output_model = build_output_model(validate_output_schema(_schema()))
    result = output_model.model_validate(
        {"outcome": "confirmed", "notes": "Patient explicitly confirmed."}
    )

    assert result.model_dump(mode="json", by_alias=True, exclude_unset=True) == {
        "outcome": "confirmed",
        "notes": "Patient explicitly confirmed.",
    }


def test_outbound_body_rejects_conflicting_agent_ids():
    with pytest.raises(ValidationError):
        OutboundCallBody.model_validate(
            {
                "channel": "phone",
                "destination": "+15551234567",
                "bot_config_id": "agent-a",
                "live_agent": {"agent_id": "agent-b"},
            }
        )


def test_merge_template_tools_handles_dicts_and_objects():
    defaults = [{"name": "end_call", "type": "builtin"}]
    assert _merge_template_tools(defaults, None, []) == ["end_call"]
    assert _merge_template_tools(tuple(defaults), None, []) == ["end_call"]


def test_live_agent_request_language_overrides_template_default():
    template = SimpleNamespace(
        instructions="Call the customer.",
        variables={},
        tools=(),
        settings={"language": "en-US"},
    )

    _, _, _, _, language, _ = _render_live(
        template,
        OutboundCallBody(
            channel="phone",
            destination="+15551234567",
            live_agent={"language": "te-IN"},
        ).live_agent,
    )

    assert language == "te-IN"


def test_live_agent_request_rejects_unsupported_language_tags():
    with pytest.raises(ValidationError):
        OutboundCallBody.model_validate(
            {
                "channel": "phone",
                "destination": "+15551234567",
                "live_agent": {"language": "not-a-language"},
            }
        )

