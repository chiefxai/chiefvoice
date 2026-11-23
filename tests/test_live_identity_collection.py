from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from chiefvoice.agents.call.hangup_validation import validate_hangup_request_async
from chiefvoice.agents.call.session_state import initialize_collected_data
from chiefvoice.agents.post_call.workflows.attendance.live_identity import (
    AttendanceLiveIdentityExtension,
    save_caller_identity,
)
from chiefvoice.agents.call.providers.vertex import VertexGeminiLiveProvider
from chiefvoice.sessions import AcceptedSession, Direction


def _session() -> AcceptedSession:
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        live_agent_context={"mandatory_fields": ["caregiver_identity", "patient_identity"]},
        metadata={"caller_interaction_seen": True},
    )
    AttendanceLiveIdentityExtension().configure(session)
    return session


def test_assignment_identity_is_not_caller_collected_evidence():
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        live_agent_context={
            "caregiver_identity": "Expected caregiver",
            "patient_identity": "Expected patient",
        },
    )

    assert initialize_collected_data(session) == {}


@pytest.mark.asyncio
async def test_tamil_caregiver_name_is_saved_without_transliteration():
    session = _session()

    result = await save_caller_identity(
        session,
        identity_type="caregiver_identity",
        name="குமார்",
        confidence=0.96,
        explicitly_stated=True,
    )

    assert result["status"] == "saved"
    assert session.metadata["collected_data"]["caregiver_identity"] == "குமார்"


@pytest.mark.asyncio
async def test_english_name_is_saved():
    session = _session()

    await save_caller_identity(
        session,
        identity_type="caregiver_identity",
        name="Kumar",
        confidence=0.94,
        explicitly_stated=True,
    )

    assert session.metadata["collected_data"]["caregiver_identity"] == "Kumar"


@pytest.mark.asyncio
async def test_caregiver_and_patient_names_are_saved_independently():
    session = _session()

    await save_caller_identity(
        session,
        identity_type="caregiver_identity",
        name="குமார்",
        confidence=0.95,
        explicitly_stated=True,
    )
    await save_caller_identity(
        session,
        identity_type="patient_identity",
        name="மீனா",
        confidence=0.92,
        explicitly_stated=True,
    )

    assert session.metadata["collected_data"] == {
        "caregiver_identity": "குமார்",
        "patient_identity": "மீனா",
    }


@pytest.mark.asyncio
async def test_repeat_name_returns_reused_status_and_only_missing_patient():
    session = _session()
    await save_caller_identity(
        session,
        identity_type="caregiver_identity",
        name="குமார்",
        confidence=0.95,
        explicitly_stated=True,
    )

    result = await save_caller_identity(
        session,
        identity_type="caregiver_identity",
        name="குமார்",
        confidence=0.98,
        explicitly_stated=True,
    )

    assert result["status"] == "reused"
    assert result["missing_required_identity_fields"] == ["patient_identity"]
    assert "Do not ask for it again" in result["message"]


@pytest.mark.asyncio
async def test_partial_hangup_rejection_preserves_collected_identity():
    session = _session()
    result = await validate_hangup_request_async(
        session,
        {
            "reason": "completed",
            "collected_data": {"caregiver_identity": "குமார்"},
        },
    )

    assert not result.allowed
    assert result.missing_fields == ["patient_identity"]
    assert session.metadata["collected_data"]["caregiver_identity"] == "குமார்"


@pytest.mark.asyncio
async def test_unverified_hangup_arguments_cannot_overwrite_saved_identity():
    session = _session()
    session.metadata["collected_data"] = {"caregiver_identity": "குமார்"}

    result = await validate_hangup_request_async(
        session,
        {
            "reason": "completed",
            "collected_data": {
                "caregiver_identity": "another possibly misheard name",
            },
        },
    )

    assert not result.allowed
    assert result.missing_fields == ["patient_identity"]
    assert session.metadata["collected_data"]["caregiver_identity"] == "குமார்"


@pytest.mark.asyncio
async def test_tamil_identity_values_pass_hangup_validation():
    session = _session()
    session.metadata["collected_data"] = {
        "caregiver_identity": "குமார்",
        "patient_identity": "மீனா",
    }

    result = await validate_hangup_request_async(session, {"reason": "completed"})

    assert result.allowed


@pytest.mark.asyncio
async def test_identity_field_must_be_a_nonempty_name_string():
    session = _session()
    session.metadata["collected_data"] = {
        "caregiver_identity": ["not", "a", "name"],
        "patient_identity": "மீனா",
    }

    result = await validate_hangup_request_async(session, {"reason": "completed"})

    assert not result.allowed
    assert result.missing_fields == ["caregiver_identity"]


@pytest.mark.asyncio
async def test_uncertain_or_empty_value_does_not_overwrite_valid_identity():
    session = _session()
    session.metadata["collected_data"] = {"caregiver_identity": "குமார்"}

    result = await save_caller_identity(
        session,
        identity_type="caregiver_identity",
        name="",
        confidence=0.2,
        explicitly_stated=False,
    )

    assert result["status"] == "uncertain"
    assert session.metadata["collected_data"]["caregiver_identity"] == "குமார்"


@pytest.mark.asyncio
async def test_extraction_and_validation_share_a_serialized_state():
    session = _session()
    from chiefvoice.agents.call.session_state import collected_data_lock

    lock = collected_data_lock(session)
    await lock.acquire()
    extraction = asyncio.create_task(
        save_caller_identity(
            session,
            identity_type="caregiver_identity",
            name="Kumar",
            confidence=0.95,
            explicitly_stated=True,
        )
    )
    await asyncio.sleep(0)
    validation = asyncio.create_task(
        validate_hangup_request_async(session, {"reason": "completed"})
    )
    await asyncio.sleep(0)
    lock.release()

    await extraction
    result = await validation

    assert not result.allowed
    assert result.missing_fields == ["patient_identity"]
    assert session.metadata["collected_data"]["caregiver_identity"] == "Kumar"


@pytest.mark.asyncio
async def test_completion_succeeds_after_both_identity_fields_are_saved():
    session = _session()
    await save_caller_identity(
        session,
        identity_type="caregiver_identity",
        name="Kumar",
        confidence=0.95,
        explicitly_stated=True,
    )
    await save_caller_identity(
        session,
        identity_type="patient_identity",
        name="Meena",
        confidence=0.95,
        explicitly_stated=True,
    )

    result = await validate_hangup_request_async(session, {"reason": "completed"})

    assert result.allowed
    assert session.metadata["collected_data"] == {
        "caregiver_identity": "Kumar",
        "patient_identity": "Meena",
    }


@pytest.mark.asyncio
async def test_attendance_workflow_registers_identity_tool(monkeypatch):
    monkeypatch.setattr(
        "pipecat.services.google.gemini_live.vertex.llm.GeminiLiveVertexLLMService._get_credentials",
        lambda *args, **kwargs: MagicMock(),
    )
    session = _session()
    session.auto_hangup_when_complete = True
    session.post_call_workflow_id = "attendance"
    provider = VertexGeminiLiveProvider(
        project_id="test-project",
        location="us-central1",
        model="google/gemini-live-2.5-flash-native-audio",
        voice="Charon",
        system_instruction="Test instructions",
        live_tool_extensions={"attendance": AttendanceLiveIdentityExtension()},
    )
    service = (await provider.create_processors(session))[0]
    tool_names = {
        tool.name for tool in service._service_tools().standard_tools
    }

    assert "record_caller_identity" in tool_names
    assert {"end_call", "hang_up_call"}.issubset(tool_names)


@pytest.mark.asyncio
async def test_unregistered_workflow_does_not_receive_attendance_tool_or_fields(monkeypatch):
    monkeypatch.setattr(
        "pipecat.services.google.gemini_live.vertex.llm.GeminiLiveVertexLLMService._get_credentials",
        lambda *args, **kwargs: MagicMock(),
    )
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        auto_hangup_when_complete=True,
        post_call_workflow_id="generic",
        live_agent_completion_requirements={"always": ["account_number"]},
    )
    provider = VertexGeminiLiveProvider(
        project_id="test-project",
        location="us-central1",
        model="google/gemini-live-2.5-flash-native-audio",
        voice="Charon",
        system_instruction="Test instructions",
        live_tool_extensions={"attendance": AttendanceLiveIdentityExtension()},
    )

    service = (await provider.create_processors(session))[0]
    names = {tool.name for tool in service._service_tools().standard_tools}

    assert "record_caller_identity" not in names
    assert "workflow_protected_fields" not in session.metadata
    assert "workflow_string_fields" not in session.metadata
