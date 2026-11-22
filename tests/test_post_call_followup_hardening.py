from __future__ import annotations

import logging
import gc
from types import SimpleNamespace
import weakref

import pytest
from cryptography.fernet import Fernet

from chiefvoice.agents.call.evidence import capture_tool_execution
from chiefvoice.agents.post_call.base import CompletedCall, PostCallResult
from chiefvoice.agents.post_call.langchain_agent import LangChainPostCallAgent
from chiefvoice.agents.post_call.providers.api_key_vertex import ApiKeyVertexPostCallAgent
from chiefvoice.agents.post_call.workflows import PostCallWorkflowRegistry
from chiefvoice.agents.post_call.workflows.attendance import AttendanceWorkflow
from chiefvoice.agents.post_call import lambda_handler
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


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["api_key", "tenant"])
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_wrapper_projects_result_without_retaining_agent(wrapper, status, monkeypatch):
    retained = []
    current_result = {"value": None}

    class Producer:
        async def process(self, _call):
            return current_result["value"]

        def compatibility_fields(self, value):
            return AttendanceWorkflow(None).compatibility_fields(value.workflow_result)

    def create_producer(**_kwargs):
        producer = Producer()
        retained.append(weakref.ref(producer))
        return producer

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
            create_producer,
        )
        proxy = ApiKeyVertexPostCallAgent(Directory(), model="model")
    else:
        class Directory:
            async def get_ready_project_id(self, _tenant_id):
                return "project"

        monkeypatch.setattr(
            "chiefvoice.cloud_projects.post_call.create_vertex_post_call_agent",
            create_producer,
        )
        proxy = TenantVertexPostCallAgent(Directory(), location="loc", model="model")

    for _ in range(40):
        result = PostCallResult(workflow_result=_workflow_result(status))
        current_result["value"] = result
        processed, result_agent = await proxy.process_with_producer(call)
        assert processed is result
        assert result_agent.compatibility_fields(processed) == AttendanceWorkflow(
            None
        ).compatibility_fields(processed.workflow_result)
        del processed, result_agent, result
    gc.collect()
    assert len(retained) == 40
    assert all(reference() is None for reference in retained)
    assert not hasattr(proxy, "_agents_by_result_id")


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["api_key", "tenant"])
async def test_wrapper_compatibility_exception_does_not_retain_producer(wrapper, monkeypatch):
    result = PostCallResult(workflow_result=_workflow_result())
    retained = []

    class Producer:
        async def process(self, _call):
            return result

        def compatibility_fields(self, _result):
            raise RuntimeError("compatibility projection failed")

    def create_producer(**_kwargs):
        producer = Producer()
        retained.append(weakref.ref(producer))
        return producer

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
                    post_call_project_id=None,
                    project_id="project",
                    post_call_location=None,
                    location="loc",
                    post_call_model="model",
                    post_call_credentials_json=None,
                    credentials_json=None,
                )

        monkeypatch.setattr(
            "chiefvoice.agents.post_call.providers.api_key_vertex.create_vertex_post_call_agent",
            create_producer,
        )
        proxy = ApiKeyVertexPostCallAgent(Directory(), model="model")
    else:
        class Directory:
            async def get_ready_project_id(self, _tenant_id):
                return "project"

        monkeypatch.setattr(
            "chiefvoice.cloud_projects.post_call.create_vertex_post_call_agent",
            create_producer,
        )
        proxy = TenantVertexPostCallAgent(Directory(), location="loc", model="model")

    processed, result_agent = await proxy.process_with_producer(call)
    with pytest.raises(RuntimeError, match="compatibility projection failed"):
        result_agent.compatibility_fields(processed)
    del processed, result_agent
    gc.collect()
    assert retained[0]() is None
    assert not hasattr(proxy, "_agents_by_result_id")


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
    call = CompletedCall(
        session_id="session",
        tenant_id="tenant",
        provider="test",
        channel="phone",
        direction="outbound",
    )
    processed, producer = await proxy.process_with_producer(call)
    assert processed is result
    assert producer.compatibility_fields(processed) == {
        "attendance_validation": {"outcome": "verified"}
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_point", "expected_error"),
    [
        ("api_key_initialize", "api key initialization failed"),
        ("process_job", "post-call processing failed"),
    ],
)
async def test_lambda_closes_initialized_resources_without_masking_failure(
    monkeypatch, failure_point, expected_error
):
    closed = {}

    class Resource:
        def __init__(self, name):
            self.name = name
            closed[name] = 0

        async def initialize(self):
            if self.name == "api_key_directory" and failure_point == "api_key_initialize":
                raise RuntimeError("api key initialization failed")

        async def close(self):
            closed[self.name] += 1
            raise RuntimeError("cleanup failure must not mask the primary error")

    class Queue(Resource):
        def __init__(self, *_args, **_kwargs):
            super().__init__("queue")

        async def process_job(self, _job_id, _agent):
            if failure_point == "process_job":
                raise RuntimeError("post-call processing failed")
            return True

    class SessionStore(Resource):
        def __init__(self, *_args, **_kwargs):
            super().__init__("call_session_store")

        async def get_post_call_workflow_config(self, *_args):
            return None

    class TemplateDirectory(Resource):
        def __init__(self, *_args, **_kwargs):
            super().__init__("template_directory")

    class TenantDirectory(Resource):
        def __init__(self, *_args, **_kwargs):
            super().__init__("tenant_project_directory")

        async def get_ready_project_id(self, _tenant_id):
            return "project"

    class ApiKeyDirectory(Resource):
        def __init__(self, *_args, **_kwargs):
            super().__init__("api_key_directory")

    monkeypatch.setattr(
        lambda_handler,
        "settings",
        SimpleNamespace(
            database_url="postgresql+asyncpg://unit-test",
            post_call_encryption_key="test-key",
            vertex_credentials_json=None,
            post_call_agent_provider="langchain_vertex",
            vertex_project_isolation_enabled=True,
            vertex_project_id=None,
            vertex_location="us-central1",
            credentials_encryption_key="credentials-key",
            post_call_location="us-central1",
            post_call_model="test-model",
            vertex_credentials_path=None,
        ),
    )
    for secret_name in (
        "DATABASE_URL_SECRET_ARN",
        "POST_CALL_ENCRYPTION_KEY_SECRET_ARN",
        "GOOGLE_CREDENTIALS_SECRET_ARN",
    ):
        monkeypatch.setenv(secret_name, "")
    monkeypatch.setattr(lambda_handler, "create_recording_store", lambda: object())
    monkeypatch.setattr(lambda_handler, "SqlPostCallQueue", Queue)
    monkeypatch.setattr(lambda_handler, "SqlCallSessionStore", SessionStore)
    monkeypatch.setattr(lambda_handler, "SqlAgentTemplateDirectory", TemplateDirectory)
    monkeypatch.setattr(
        lambda_handler, "SqlTenantVertexProjectDirectory", TenantDirectory
    )
    monkeypatch.setattr(lambda_handler, "SqlApiKeyDirectory", ApiKeyDirectory)
    monkeypatch.setattr(
        lambda_handler, "create_vertex_post_call_agent", lambda **_kwargs: object()
    )

    with pytest.raises(RuntimeError, match=expected_error):
        await lambda_handler._process({"schema_version": 1, "job_id": "test-job"})

    assert closed == {
        "queue": 1,
        "call_session_store": 1,
        "template_directory": 1,
        "tenant_project_directory": 1,
        "api_key_directory": 1,
    }


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


@pytest.mark.asyncio
async def test_legacy_template_workflow_recovery_requires_same_revision_and_api_scope():
    session = AcceptedSession(
        channel="phone",
        provider="test",
        direction=Direction.OUTBOUND,
        tenant_id="tenant-a",
        api_key_id="key-a",
        session_id="session-a",
        post_call_template_id="template-a",
        post_call_template_revision=4,
    )
    store = SqlCallSessionStore.__new__(SqlCallSessionStore)

    async def get_session(tenant_id, session_id):
        assert (tenant_id, session_id) == ("tenant-a", "session-a")
        return session

    store.get_session = get_session
    requested = []

    class Templates:
        async def get_for_api(self, api_key_id, template_id):
            requested.append((api_key_id, template_id))
            return SimpleNamespace(
                kind="post_call",
                revision=4,
                api_key_id="key-a",
                settings={"workflow_id": "attendance"},
            )

    resolved = await store.get_post_call_workflow_config("tenant-a", "session-a", Templates())
    assert resolved.post_call_workflow_id == "attendance"
    assert requested == [("key-a", "template-a")]

    class RevisedTemplate(Templates):
        async def get_for_api(self, api_key_id, template_id):
            return SimpleNamespace(
                kind="post_call",
                revision=5,
                api_key_id="key-a",
                settings={"workflow_id": "attendance"},
            )

    ambiguous = await store.get_post_call_workflow_config(
        "tenant-a", "session-a", RevisedTemplate()
    )
    assert ambiguous.post_call_workflow_id is None
