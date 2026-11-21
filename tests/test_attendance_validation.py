import pytest

from chiefvoice.agents.post_call.attendance_validation import validate_attendance
from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.agents.post_call.langchain_agent import LangChainPostCallAgent


def _field(value, utterance, *, confidence=0.95):
    return {
        "value": value,
        "source": "caller_transcript",
        "original_utterance": utterance,
        "confidence": confidence,
    }


def _data(
    *,
    status="arrived",
    status_text="I arrived at 09:15",
    caregiver="Kumar",
    patient="Meena",
    address="Anna Nagar",
    confirmation="Yes, I confirm the summary",
    time="09:15",
):
    return {
        "caregiver_identity": _field(caregiver, f"My name is {caregiver}"),
        "patient_identity": _field(patient, f"The patient is {patient}"),
        "patient_address": _field(address, f"The address is {address}"),
        "attendance_status": _field(status, status_text),
        "attendance_time": _field(time, status_text) if time else None,
        "caregiver_confirmation": _field(True, confirmation),
    }


def _transcript(status_text="I arrived at 09:15", confirmation="Yes, I confirm the summary"):
    return "\n".join(
        (
            "Caller: My name is Kumar",
            "Caller: The patient is Meena",
            "Caller: The address is Anna Nagar",
            f"Caller: {status_text}",
            "Assistant: Summary: caregiver Kumar, patient Meena, address Anna Nagar, attendance details recorded. Please confirm.",
            f"Caller: {confirmation}",
        )
    )


def _validate(
    data=None,
    *,
    status_text="I arrived at 09:15",
    confirmation="Yes, I confirm the summary",
    expected=None,
    claim=None,
    transcript=None,
    extraction_status="clear",
):
    return validate_attendance(
        extracted=data or _data(status_text=status_text, confirmation=confirmation),
        transcript=_transcript(status_text, confirmation) if transcript is None else transcript,
        expected=expected
        or {
            "caregiver_identity": "Kumar",
            "patient_identity": "Meena",
            "patient_address": "Anna Nagar",
        },
        live_claim=claim,
        extraction_status=extraction_status,
    )


def test_fully_verified_check_in():
    assert _validate()["outcome"] == "verified"


def test_fully_verified_check_out():
    text = "I departed at 17:30"
    result = _validate(
        data=_data(status="departed", status_text=text, time="17:30"),
        status_text=text,
        claim={"collected_data": {"attendance_type": "check_out"}},
    )
    assert result["outcome"] == "verified"


def test_garbled_caller_response_is_unclear():
    data = _data(status="arrived", status_text="Woher?")
    result = _validate(data=data, status_text="Woher?")
    assert result["outcome"] == "unclear"
    assert result["requires_manual_review"]


def test_garbled_response_after_other_clear_caller_turns_is_unclear():
    data = _data()
    data["attendance_status"] = None
    transcript = _transcript().replace("Caller: I arrived at 09:15", "Caller: Woher?")
    result = _validate(data=data, transcript=transcript)
    assert result["outcome"] == "unclear"


def test_expected_names_without_caller_evidence_are_unverified():
    result = _validate(transcript="Caller: Yes, I confirm the summary")
    assert result["outcome"] == "incomplete"
    assert result["fields"]["caregiver_identity"]["value"] is None


def test_assistant_repeating_expected_names_is_not_evidence():
    transcript = "\n".join(
        (
            "Assistant: Kumar, Meena at Anna Nagar, you have arrived.",
            "Caller: Yes, I confirm the summary",
        )
    )
    result = _validate(transcript=transcript)
    assert result["outcome"] == "incomplete"


def test_wrong_caregiver_identity_is_mismatch():
    data = _data(caregiver="Ravi")
    transcript = _transcript().replace("My name is Kumar", "My name is Ravi")
    result = _validate(data=data, transcript=transcript)
    assert "caregiver_identity" in result["mismatched_fields"]
    assert result["outcome"] == "identity_mismatch"


def test_wrong_patient_identity_is_mismatch():
    data = _data(patient="Rani")
    transcript = _transcript().replace("patient is Meena", "patient is Rani")
    assert "patient_identity" in _validate(data=data, transcript=transcript)["mismatched_fields"]


def test_wrong_address_is_mismatch():
    data = _data(address="T Nagar")
    transcript = _transcript().replace("Anna Nagar", "T Nagar")
    result = _validate(data=data, transcript=transcript)
    assert "patient_address" in result["mismatched_fields"]


def test_partial_identity_is_incomplete():
    data = _data()
    data["patient_identity"] = None
    result = _validate(data=data)
    assert result["outcome"] == "incomplete"


def test_arrival_without_actual_time_can_verify_status_but_time_is_null():
    data = _data(status_text="I have arrived", time=None)
    result = _validate(data=data, status_text="I have arrived")
    assert result["outcome"] == "verified"
    assert result["fields"]["attendance_time"]["value"] is None
    assert "actual_attendance_time_unverified" in result["validation_errors"]


def test_not_arrived_with_callback_requested():
    text = "I have not arrived; please call me back later"
    data = _data(status="not_arrived", status_text=text, time=None)
    result = _validate(
        data=data, status_text=text, claim={"collected_data": {"attendance_type": "check_in"}}
    )
    assert result["outcome"] == "callback_required"


def test_tamil_pronunciation_variation_matches_expected_name():
    text = "நான் வந்துவிட்டேன் 09:15"
    data = _data(status="arrived", status_text=text)
    data["caregiver_identity"] = _field("குமார", "என் பெயர் குமார")
    transcript = "\n".join(
        (
            "Caller: என் பெயர் குமார",
            "Caller: நோயாளி மீனா",
            "Caller: முகவரி அண்ணா நகர்",
            f"Caller: {text}",
            "Assistant: சுருக்கம் இதுதான், உறுதிப்படுத்துங்கள்",
            "Caller: ஆம், சுருக்கம் சரி",
        )
    )
    data["patient_identity"] = _field("மீனா", "நோயாளி மீனா")
    data["patient_address"] = _field("அண்ணா நகர்", "முகவரி அண்ணா நகர்")
    data["caregiver_confirmation"] = _field(True, "ஆம், சுருக்கம் சரி")
    expected = {
        "caregiver_identity": "குமார்",
        "patient_identity": "மீனா",
        "patient_address": "அண்ணா நகர்",
    }
    result = _validate(data=data, transcript=transcript, expected=expected)
    assert result["fields"]["caregiver_identity"]["verification_status"] == "verified"


def test_identity_tool_event_is_accepted_only_with_matching_caller_turn():
    text = "My name is Kumar"
    data = _data()
    data["caregiver_identity"] = {
        "value": "Kumar",
        "source": "verified_tool_event",
        "original_utterance": text,
        "confidence": 0.95,
    }
    event = {
        "arguments": {"identity_type": "caregiver_identity", "name": "Kumar"},
        "caller_transcript_at_execution": f"Caller: {text}",
    }
    event["arguments"].update({"explicitly_stated": True, "confidence": 0.95})
    result = _validate(data=data, transcript=_transcript(), claim=None)
    # Without the event, the explicit caller turn itself still supports the extraction.
    assert result["fields"]["caregiver_identity"]["verification_status"] == "verified"
    event_result = validate_attendance(
        extracted=data,
        transcript=_transcript(),
        expected={"caregiver_identity": "Kumar"},
        tool_events=(event,),
    )
    assert event_result["fields"]["caregiver_identity"]["source"] == "verified_tool_event"


def test_caller_yes_without_assistant_final_summary_is_not_confirmation():
    text = "Yes, I confirm the summary"
    transcript = _transcript().replace(
        "Assistant: Summary: caregiver Kumar, patient Meena, address Anna Nagar, attendance details recorded. Please confirm.\n",
        "",
    )
    result = _validate(transcript=transcript)
    assert result["outcome"] == "incomplete"
    assert "explicit_summary_confirmation_missing" in result["validation_errors"]


def test_configured_distinctive_locality_policy_allows_locality_only_match():
    data = _data(address="Anna Nagar")
    result = _validate(
        data=data,
        expected={
            "caregiver_identity": "Kumar",
            "patient_identity": "Meena",
            "patient_address": "12, North Street, Anna Nagar",
            "address_match_policy": "distinctive_locality",
            "patient_locality": "Anna Nagar",
        },
    )
    assert result["fields"]["patient_address"]["verification_status"] == "verified"


class _Invoker:
    def __init__(self, model):
        self.model = model

    async def ainvoke(self, _messages, config=None):
        return self.model.responses.pop(0)


class _Model:
    def __init__(self, *responses):
        self.responses = list(responses)

    def with_structured_output(self, _schema):
        return _Invoker(self)


@pytest.mark.asyncio
async def test_post_call_agent_runs_python_validation_after_attendance_extraction():
    model = _Model(
        {
            "transcript_status": "clear",
            "summary": None,
            "disposition": None,
            "extracted_data": {"hallucinated": "must be filtered"},
            "action_items": [],
            "needs_audio_review": False,
        },
        {"transcript_status": "clear", "extracted_data": _data()},
    )
    call = CompletedCall(
        session_id="attendance-test",
        tenant_id="tenant",
        provider="vobiz",
        channel="phone",
        direction="outbound",
        transcript=_transcript(),
        expected_assignment={
            "caregiver_identity": "Kumar",
            "patient_identity": "Meena",
            "patient_address": "Anna Nagar",
        },
    )
    result = await LangChainPostCallAgent(model).process(call)
    assert result.attendance_validation["outcome"] == "verified"
    assert result.summary
    assert result.disposition == "verified"
    assert "hallucinated" not in result.extracted_data


def test_missing_transcript_is_unclear():
    result = _validate(transcript="", extraction_status="no_data")
    assert result["outcome"] == "unclear"
    assert result["requires_manual_review"]


def test_live_claim_conflict_is_reported_without_overwrite():
    result = _validate(
        claim={"collected_data": {"status": "confirmed", "patient_identity": "Other"}}
    )
    assert result["outcome"] == "verified"
    assert result["is_consistent"] is False
    assert "patient_identity" in result["mismatched_fields"]
