import pytest
from pydantic import ValidationError

from chiefvoice.agents.post_call.output_schema import build_output_model, validate_output_schema
from chiefvoice.calls.routes import OutboundCallBody


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
            "provider": "vobiz",
            "channel": "phone",
            "destination": "+15551234567",
            "live_agent": {
                "agent_id": "agent-a",
                "context": {"patient_name": "Alex"},
            },
            "post_call": {
                "context": "Extract the confirmed appointment outcome.",
                "output_schema": _schema(),
            },
        }
    )

    assert body.live_agent.context == {"patient_name": "Alex"}
    assert body.post_call.context == "Extract the confirmed appointment outcome."
    assert body.post_call.output_schema.type == "object"


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
                "provider": "vobiz",
                "channel": "phone",
                "destination": "+15551234567",
                "bot_config_id": "agent-a",
                "live_agent": {"agent_id": "agent-b"},
            }
        )
