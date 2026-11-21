from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from chiefvoice.agents.call.evidence import capture_tool_execution
from chiefvoice.agents.post_call.base import CompletedCall, PostCallResult
from chiefvoice.agents.post_call.langchain_agent import LangChainPostCallAgent
from chiefvoice.agents.post_call.providers.api_key_vertex import ApiKeyVertexPostCallAgent
from chiefvoice.agents.post_call.workflows import PostCallWorkflowRegistry
from chiefvoice.agents.post_call.workflows.attendance import AttendanceWorkflow
from chiefvoice.calls.store import SqlCallSessionStore
from chiefvoice.sessions import AcceptedSession, Direction
from chiefvoice.cloud_projects.post_call import TenantVertexPostCallAgent


class _NoModel:
    def with_structured_output(self, _schema):
        return self

    async def ainvoke(self, *_args, **_kwargs):
        return {}


def _workflow_result(status="completed"):
    if status == "completed":
        data = {"outcome": "verified", "requires_manual_review": False}
        return {"version": 1, "workflow_id": "attendance", "status": status, "data": data}
    return {
        "version": 1,
        "workflow_id": "attendance",
        "status": "failed",
        "error_code": "workflow_execution_failed",
    }


@pytest.mark.parametrize("wrapper", ["api_key", "tenant"])
@pytest.mark.parametrize("status", ["completed", "failed"])
def test_provider_wrappers_forward_attendance_legacy_projection(wrapper, status):
    result = PostCallResult(workflow_result=_workflow_result(status))
    producer = SimpleNamespace(
        compatibility_fields=lambda value: AttendanceWorkflow(None).compatibility_fields(
            value.workflow_result
        )
    )
    if wrapper == "api_key":
        proxy = ApiKeyVertexPostCallAgent(SimpleNamespace(), model="model")
    else:
        proxy = TenantVertexPostCallAgent(SimpleNamespace(), location="loc", model="model")
    proxy._agents_by_result_id[id(result)] = producer
    projection = proxy.compatibility_fields(result)
    assert projection == AttendanceWorkflow(None).compatibility_fields(result.workflow_result)
    assert "attendance_validation" in projection


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["api_key", "tenant"])
async def test_wrapper_process_retains_producing_agent_for_projection(wrapper, monkeypatch):
    result = PostCallResult(workflow_result=_workflow_result())

    class Producer:
        async def process(self, _call):
            return result

        def compatibility_fields(self, value):
            return AttendanceWorkflow(None).compatibility_fields(value.workflow_result)

    producer = Producer()
    call = CompletedCall(
        session_id="session",
        tenant_id="tenant",
        provider="test",
        channel="phone",
        direction="outbound",
        api_key_id="key-id",
    )
    if wrapper == "api_key":
        class Directory:
            async def get_vertex_config(self, _key_id):
                return SimpleNamespace(
                    post_call_provider="vertex",
                    post_call_project_id="project",
                    project_id="project",
                    post_call_location="loc",
                    location="loc",
                    post_call_model="model",
                    post_call_credentials_json=None,
                    credentials_json=None,
                )

        monkeypatch.setattr(
            "chiefvoice.agents.post_call.providers.api_key_vertex.create_vertex_post_call_agent",
            lambda **_kwargs: producer,
        )
        proxy = ApiKeyVertexPostCallAgent(Directory(), model="model")
    else:
        class Directory:
            async def get_ready_project_id(self, _tenant_id):
                return "project"

        monkeypatch.setattr(
            "chiefvoice.cloud_projects.post_call.create_vertex_post_call_agent",
            lambda **_kwargs: producer,
        )
        proxy = TenantVertexPostCallAgent(Directory(), location="loc", model="model")

    processed = await proxy.process(call)
    assert processed is result
    assert proxy.compatibility_fields(processed) == {
        "attendance_validation": {"outcome": "verified", "requires_manual_review": False}
    }


def test_capture_tool_execution_deep_copies_nested_arguments():
    session = AcceptedSession(channel="phone", provider="test", direction=Direction.OUTBOUND)
    arguments = {"identity": {"name": "Kumar", "labels": ["caller"]}}
    capture_tool_execution(session, tool_name="record_caller_identity", arguments=arguments)
    arguments["identity"]["name"] = "Assistant mutation"
    arguments["identity"]["labels"].append("changed")
    assert session.metadata["tool_events"][0]["arguments"] == {
        "identity": {"name": "Kumar", "labels": ["caller"]}
    }


class _FailingWorkflow:
    workflow_id = "sensitive.failure"

    async def process(self, _call, _result):
        raise RuntimeError("secret-token transcript and prompt leaked")


@pytest.mark.asyncio
async def test_workflow_error_log_excludes_provider_exception_details(caplog):
    registry = PostCallWorkflowRegistry()
    registry.register(_FailingWorkflow())
    call = CompletedCall(
        session_id="session-secret",
        tenant_id="tenant-secret",
        provider="test",
        channel="phone",
        direction="outbound",
        transcript="caller private transcript",
        workflow_id="sensitive.failure",
    )
    agent = LangChainPostCallAgent(_NoModel(), workflow_registry=registry)
    with caplog.at_level(logging.ERROR, logger="chiefvoice.agents.post_call.langchain_agent"):
        result = await agent._run_workflow(call, PostCallResult())
    assert result["status"] == "failed"
    assert "secret-token" not in caplog.text
    assert "transcript and prompt" not in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
async def test_provider_compatibility_fallback_path_forwards_projection():
    result = PostCallResult(workflow_result=_workflow_result())

    class Fallback:
        async def process(self, _call):
            return result

        def compatibility_fields(self, _result):
            return {"attendance_validation": {"outcome": "verified"}}

    proxy = ApiKeyVertexPostCallAgent(SimpleNamespace(), model="model", fallback=Fallback())
    assert proxy.compatibility_fields(result) == {"attendance_validation": {"outcome": "verified"}}


def test_session_workflow_configuration_is_tenant_and_session_scoped():
    # Exercise the authoritative resolver used by legacy queue recovery at its
    # storage boundary through get_session's construction contract.
    session = AcceptedSession(
        channel="phone",
        provider="test",
        direction=Direction.OUTBOUND,
        tenant_id="tenant-a",
        session_id="session-a",
        post_call_workflow_id="attendance",
        post_call_workflow_context={"assignment": "persisted"},
    )
    store = SqlCallSessionStore.__new__(SqlCallSessionStore)
    store._sessions = None
    store._fernet = Fernet(Fernet.generate_key())
    recovered = store._decrypt_session(store._encrypt_session(session))
    assert recovered.tenant_id == "tenant-a"
    assert recovered.session_id == "session-a"
    assert recovered.post_call_workflow_id == "attendance"
    assert recovered.post_call_workflow_context == {"assignment": "persisted"}
