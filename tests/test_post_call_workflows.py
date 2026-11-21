from collections import deque

import pytest

from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.agents.post_call.langchain_agent import LangChainPostCallAgent
from chiefvoice.agents.post_call.workflows import PostCallWorkflowRegistry
from chiefvoice.agents.post_call.fallback_spool import _completed_call


class _Invoker:
    def __init__(self, model):
        self.model = model

    async def ainvoke(self, _messages, config=None):
        return self.model.responses.popleft()


class _Model:
    def __init__(self, *responses):
        self.responses = deque(responses)

    def with_structured_output(self, _schema):
        return _Invoker(self)


class _Workflow:
    workflow_id = "test.workflow"

    def __init__(self, *, fail=False):
        self.calls = []
        self.fail = fail

    async def process(self, call, result):
        self.calls.append((call.session_id, call.tenant_id, result.summary))
        if self.fail:
            raise RuntimeError("workflow unavailable")
        return {"accepted": True}


class _MutatingWorkflow:
    workflow_id = "test.mutating"

    def __init__(self, *, fail=False, return_alias=False):
        self.fail = fail
        self.return_alias = return_alias

    async def process(self, _call, result):
        result.structured_output["nested"]["value"] = "workflow mutation"
        if self.fail:
            raise RuntimeError("failed after mutation")
        if self.return_alias:
            return {"echo": result.structured_output}
        return {"accepted": True}


def _generic_response():
    return {
        "transcript_status": "clear",
        "summary": "Caller provided a response.",
        "disposition": "completed",
        "extracted_data": {},
        "action_items": [],
        "needs_audio_review": False,
    }


def _call(**kwargs):
    values = {
        "session_id": "session-a",
        "tenant_id": "tenant-a",
        "provider": "vobiz",
        "channel": "phone",
        "direction": "outbound",
        "transcript": "Caller: okay",
    }
    values.update(kwargs)
    return CompletedCall(**values)


def _custom_schema_call(**kwargs):
    return _call(
        output_schema={
            "type": "object",
            "properties": {
                "ticket": {"type": "string"},
                "nested": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            },
            "required": ["ticket", "nested"],
        },
        **kwargs,
    )


def _custom_response():
    return {
        "structured_output": {
            "ticket": "T-7",
            "nested": {"value": "caller result"},
        },
        "transcript_status": "clear",
        "needs_audio_review": False,
    }


@pytest.mark.asyncio
async def test_workflow_dispatch_is_explicit_and_keeps_tenant_scope():
    workflow = _Workflow()
    registry = PostCallWorkflowRegistry()
    registry.register(workflow)
    call = _call(workflow_id="test.workflow", workflow_context={"tenant_ref": "tenant-a"})

    result = await LangChainPostCallAgent(
        _Model(_generic_response()), workflow_registry=registry
    ).process(call)

    assert workflow.calls == [("session-a", "tenant-a", "Caller provided a response.")]
    assert result.workflow_result == {
        "version": 1,
        "workflow_id": "test.workflow",
        "status": "completed",
        "data": {"accepted": True},
    }

    second_call = CompletedCall(
        session_id="session-b",
        tenant_id="tenant-b",
        provider="vobiz",
        channel="phone",
        direction="outbound",
        transcript="Caller: okay",
        workflow_id="test.workflow",
        workflow_context={"tenant_ref": "tenant-b"},
    )
    await LangChainPostCallAgent(
        _Model(_generic_response()), workflow_registry=registry
    ).process(second_call)
    assert workflow.calls[-1][:2] == ("session-b", "tenant-b")


@pytest.mark.asyncio
async def test_prompt_text_never_selects_a_workflow():
    workflow = _Workflow()
    registry = PostCallWorkflowRegistry()
    registry.register(workflow)
    call = _call(post_call_context="This is check-in for a caregiver and patient.")

    result = await LangChainPostCallAgent(
        _Model(_generic_response()), workflow_registry=registry
    ).process(call)

    assert workflow.calls == []
    assert result.workflow_result is None


@pytest.mark.asyncio
async def test_workflow_failure_isolated_from_generic_post_call_result():
    workflow = _Workflow(fail=True)
    registry = PostCallWorkflowRegistry()
    registry.register(workflow)
    result = await LangChainPostCallAgent(
        _Model(_generic_response()), workflow_registry=registry
    ).process(_call(workflow_id="test.workflow"))

    assert result.summary == "Caller provided a response."
    assert result.structured_output is None
    assert result.workflow_result == {
        "version": 1,
        "workflow_id": "test.workflow",
        "status": "failed",
        "error_code": "workflow_execution_failed",
    }


@pytest.mark.asyncio
async def test_custom_schema_without_workflow_keeps_legacy_result_shape():
    result = await LangChainPostCallAgent(_Model(_custom_response())).process(
        _custom_schema_call()
    )

    assert result.structured_output == {
        "ticket": "T-7",
        "nested": {"value": "caller result"},
    }
    assert result.workflow_result is None


@pytest.mark.asyncio
async def test_custom_schema_with_successful_workflow_keeps_generic_output():
    registry = PostCallWorkflowRegistry()
    registry.register(_Workflow())
    result = await LangChainPostCallAgent(
        _Model(_custom_response()), workflow_registry=registry
    ).process(_custom_schema_call(workflow_id="test.workflow"))

    assert result.structured_output == {
        "ticket": "T-7",
        "nested": {"value": "caller result"},
    }
    assert result.workflow_result == {
        "version": 1,
        "workflow_id": "test.workflow",
        "status": "completed",
        "data": {"accepted": True},
    }


@pytest.mark.asyncio
async def test_workflow_mutation_cannot_change_custom_structured_output():
    registry = PostCallWorkflowRegistry()
    workflow = _MutatingWorkflow()
    registry.register(workflow)
    result = await LangChainPostCallAgent(
        _Model(_custom_response()), workflow_registry=registry
    ).process(_custom_schema_call(workflow_id=workflow.workflow_id))

    assert result.structured_output["nested"]["value"] == "caller result"
    assert result.workflow_result["data"] == {"accepted": True}


@pytest.mark.asyncio
async def test_mutation_then_workflow_exception_cannot_change_custom_output():
    registry = PostCallWorkflowRegistry()
    workflow = _MutatingWorkflow(fail=True)
    registry.register(workflow)
    result = await LangChainPostCallAgent(
        _Model(_custom_response()), workflow_registry=registry
    ).process(_custom_schema_call(workflow_id=workflow.workflow_id))

    assert result.structured_output["nested"]["value"] == "caller result"
    assert result.workflow_result == {
        "version": 1,
        "workflow_id": workflow.workflow_id,
        "status": "failed",
        "error_code": "workflow_execution_failed",
    }


@pytest.mark.asyncio
async def test_workflow_result_data_cannot_alias_structured_output():
    registry = PostCallWorkflowRegistry()
    workflow = _MutatingWorkflow(return_alias=True)
    registry.register(workflow)
    result = await LangChainPostCallAgent(
        _Model(_custom_response()), workflow_registry=registry
    ).process(_custom_schema_call(workflow_id=workflow.workflow_id))

    assert result.structured_output["nested"]["value"] == "caller result"
    assert result.workflow_result["data"]["echo"]["nested"]["value"] == "workflow mutation"
    result.workflow_result["data"]["echo"]["nested"]["value"] = "extension changed"
    assert result.structured_output["nested"]["value"] == "caller result"


@pytest.mark.asyncio
async def test_unregistered_workflow_fails_only_the_extension():
    result = await LangChainPostCallAgent(_Model(_generic_response())).process(
        _call(workflow_id="missing.workflow")
    )

    assert result.summary == "Caller provided a response."
    assert result.workflow_result["status"] == "failed"
    assert result.workflow_result["error_code"] == "workflow_not_registered"


@pytest.mark.asyncio
async def test_explicit_workflow_receives_no_data_without_breaking_call_result():
    workflow = _Workflow()
    registry = PostCallWorkflowRegistry()
    registry.register(workflow)
    call = _call(workflow_id="test.workflow", transcript=None)

    result = await LangChainPostCallAgent(_Model(), workflow_registry=registry).process(call)

    assert result.transcript_status == "no_data"
    assert result.workflow_result["status"] == "completed"
    assert workflow.calls == [("session-a", "tenant-a", None)]


def test_legacy_queued_call_payload_does_not_auto_select_attendance():
    call = _completed_call(
        {
            "session_id": "legacy-session",
            "tenant_id": "legacy-tenant",
            "provider": "vobiz",
            "channel": "phone",
            "direction": "outbound",
            "transcript": "Caller: data",
            "expected_assignment": {"patient_name": "Legacy patient"},
            "live_call_claim": {"reason": "completed"},
            "identity_tool_events": [{"arguments": {"name": "Legacy patient"}}],
        }
    )

    assert call.workflow_id is None
    assert call.workflow_context is None
    assert call.end_call_claim == {"reason": "completed"}
    assert call.tool_events[0]["arguments"]["name"] == "Legacy patient"
