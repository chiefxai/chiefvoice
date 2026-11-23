from __future__ import annotations

import json
import urllib.parse
from contextlib import AbstractAsyncContextManager
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from chiefvoice.agents.post_call import queue as queue_module
from chiefvoice.agents.post_call.base import CompletedCall, PostCallResult
from chiefvoice.agents.post_call.langchain_agent import LangChainPostCallAgent
from chiefvoice.agents.post_call.queue import (
    PostCallJobRow,
    PostCallWebhookConfigRow,
    PostCallWebhookDeliveryRow,
    SqlPostCallQueue,
)
from chiefvoice.agents.post_call.replay import ReplayNotFound, SqlPostCallReplayService
from chiefvoice.agents.post_call.workflows import PostCallWorkflowRegistry
from chiefvoice.agents.post_call.workflows.attendance import AttendanceWorkflow
from chiefvoice.agents.post_call.workflows.attendance.validation import validate_attendance
from chiefvoice.calls.store import CallSessionRow, SqlCallSessionStore
from chiefvoice.sessions import AcceptedSession, Direction


class _AsyncSession:
    def __init__(self, session):
        self._session = session

    async def scalar(self, statement):
        return self._session.scalar(statement)

    async def scalars(self, statement):
        return self._session.scalars(statement)

    async def execute(self, statement):
        return self._session.execute(statement)

    async def get(self, *args, **kwargs):
        return self._session.get(*args, **kwargs)

    def add(self, instance):
        self._session.add(instance)

    def begin(self):
        return _TransactionContext(self._session)


class _TransactionContext(AbstractAsyncContextManager):
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        self._transaction = self._session.begin()
        self._transaction.__enter__()
        return _AsyncSession(self._session)

    async def __aexit__(self, exc_type, exc, traceback):
        self._transaction.__exit__(exc_type, exc, traceback)
        return False


class _SessionContext(AbstractAsyncContextManager):
    def __init__(self, factory, *, transaction):
        self._factory = factory
        self._transactional = transaction

    async def __aenter__(self):
        self._sync_session = self._factory()
        self._transaction = self._sync_session.begin() if self._transactional else None
        if self._transaction is not None:
            self._transaction.__enter__()
        return _AsyncSession(self._sync_session)

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            if self._transaction is not None:
                self._transaction.__exit__(exc_type, exc, traceback)
        finally:
            self._sync_session.close()
        return False


class _SessionFactory:
    def __init__(self, factory):
        self._factory = factory

    def __call__(self):
        return _SessionContext(self._factory, transaction=False)

    def begin(self):
        return _SessionContext(self._factory, transaction=True)


@pytest.fixture
def persisted(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'chiefvoice-test.db'}")
    for table in (
        CallSessionRow.__table__,
        PostCallJobRow.__table__,
        PostCallWebhookConfigRow.__table__,
        PostCallWebhookDeliveryRow.__table__,
    ):
        table.create(engine)
    factory = _SessionFactory(sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(queue_module, "insert", sqlite_insert)
    try:
        yield factory, engine
    finally:
        engine.dispose()


class _Response:
    def __init__(self, status_code=204):
        self.status_code = status_code

    async def aclose(self):
        pass


class _HttpClient:
    sent = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def build_request(self, method, url, *, content, **_kwargs):
        return SimpleNamespace(method=method, url=url, content=content)

    async def send(self, request, *, stream):
        self.sent.append((request, stream))
        return _Response()


class _Invoker:
    def __init__(self, model):
        self._model = model

    async def ainvoke(self, *_args, **_kwargs):
        response = (
            self._model.responses.popleft()
            if self._model.responses
            else self._model.response
        )
        if isinstance(response, Exception):
            raise response
        return response


class _Model:
    response = {
        "structured_output": {
            "ticket": "T-7",
            "nested": {"value": "caller result"},
        },
        "transcript_status": "clear",
        "needs_audio_review": False,
    }

    def with_structured_output(self, _schema):
        return _Invoker(self)

    def __init__(self, responses=()):
        self.responses = deque(responses)


class _FailingMutatingWorkflow:
    workflow_id = "test.persist"

    async def process(self, _call, result):
        result.structured_output["nested"]["value"] = "mutated"
        raise RuntimeError("expected workflow failure")


class _RaisingWorkflow:
    workflow_id = "test.generic-failure"

    async def process(self, _call, _result):
        raise RuntimeError("workflow is unavailable")


def _schema():
    return {
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
    }


def _new_queue(factory, key):
    queue = SqlPostCallQueue.__new__(SqlPostCallQueue)
    queue._sessions = factory
    queue._fernet = Fernet(key.encode("ascii"))
    queue.recording_store = None
    queue.lease_duration = timedelta(minutes=2)
    queue.max_attempts = 4
    queue.max_delivery_attempts = 4
    return queue


def _call(session_id, *, tenant="tenant-b", workflow_id="test.persist"):
    return CompletedCall(
        session_id=session_id,
        tenant_id=tenant,
        provider="vobiz",
        channel="phone",
        direction="outbound",
        transcript="Caller: ticket T-7",
        output_schema=_schema(),
        workflow_id=workflow_id,
        workflow_context={"session_marker": session_id},
        end_call_claim={"reason": "completed"},
        tool_events=({"tool": "record_caller_identity", "result": "captured"},),
    )


def _attendance_transcript():
    return "\n".join(
        (
            "Caller: My name is Kumar",
            "Caller: The patient is Meena",
            "Caller: The address is Anna Nagar",
            "Caller: I arrived at 09:15",
            "Assistant: Summary: caregiver, patient, address and arrival recorded. Please confirm.",
            "Caller: Yes, I confirm the summary",
        )
    )


def _attendance_extraction():
    def evidence(value, utterance):
        return {
            "value": value,
            "source": "caller_transcript",
            "original_utterance": utterance,
            "confidence": 0.95,
        }

    status_utterance = "I arrived at 09:15"
    return {
        "transcript_status": "clear",
        "extracted_data": {
            "caregiver_identity": evidence("Kumar", "My name is Kumar"),
            "patient_identity": evidence("Meena", "The patient is Meena"),
            "patient_address": evidence("Anna Nagar", "The address is Anna Nagar"),
            "attendance_status": evidence("arrived", status_utterance),
            "attendance_time": evidence("09:15", status_utterance),
            "caregiver_confirmation": evidence(
                True, "Yes, I confirm the summary"
            ),
        },
    }


def _expected_attendance_validation():
    def field(value, utterance):
        return {
            "value": value,
            "source": "caller_transcript",
            "original_utterance": utterance,
            "confidence": 0.95,
            "verification_status": "verified",
        }

    status_utterance = "I arrived at 09:15"
    return {
        "outcome": "verified",
        "fields": {
            "caregiver_identity": field("Kumar", "My name is Kumar"),
            "patient_identity": field("Meena", "The patient is Meena"),
            "patient_address": field("Anna Nagar", "The address is Anna Nagar"),
            "attendance_status": field("arrived", status_utterance),
            "attendance_time": field("09:15", status_utterance),
            "caregiver_confirmation": field(True, "Yes, I confirm the summary"),
        },
        "is_consistent": True,
        "mismatched_fields": [],
        "validation_errors": [],
        "requires_manual_review": False,
    }


def _failed_attendance_validation():
    fields = {
        name: {
            "value": None,
            "source": None,
            "original_utterance": None,
            "confidence": 0.0,
            "verification_status": "unverified",
        }
        for name in (
            "caregiver_identity",
            "patient_identity",
            "patient_address",
            "attendance_status",
            "attendance_time",
            "caregiver_confirmation",
        )
    }
    return {
        "outcome": "unclear",
        "fields": fields,
        "is_consistent": False,
        "mismatched_fields": [],
        "validation_errors": ["attendance_workflow_failed"],
        "requires_manual_review": True,
    }


@pytest.mark.asyncio
async def test_encrypted_session_context_is_tenant_and_session_scoped(persisted):
    factory, engine = persisted
    key = Fernet.generate_key().decode("ascii")
    store = SqlCallSessionStore.__new__(SqlCallSessionStore)
    store._sessions = factory
    store._fernet = Fernet(key.encode("ascii"))
    now = datetime.now(UTC)
    for sid, tenant, marker in (
        ("session-b", "tenant-b", "only-b"),
        ("session-c", "tenant-c", "only-c"),
    ):
        accepted = AcceptedSession(
            session_id=sid,
            tenant_id=tenant,
            channel="phone",
            provider="vobiz",
            direction=Direction.OUTBOUND,
            post_call_workflow_id="test.persist",
            post_call_workflow_context={"marker": marker},
            started_at=now,
        )
        with sessionmaker(engine).begin() as db:
            db.add(
                CallSessionRow(
                    session_id=sid,
                    tenant_id=tenant,
                    request_fingerprint="fingerprint",
                    status="active",
                    encrypted_session=store._encrypt_session(accepted),
                    encrypted_transcript=None,
                    transcript_characters=0,
                    transcript_persistence_failed=False,
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )

    assert await store.get_session("tenant-a", "session-b") is None
    assert await store.get_session("tenant-b", "session-c") is None
    assert (await store.get_session("tenant-b", "session-b")).post_call_workflow_context == {
        "marker": "only-b"
    }
    assert (await store.get_session("tenant-c", "session-c")).post_call_workflow_context == {
        "marker": "only-c"
    }
    await store.append_transcript("session-b", "tenant-a", "Caller", "attacker")
    with sessionmaker(engine)() as db:
        row = db.scalar(select(CallSessionRow).where(CallSessionRow.session_id == "session-b"))
        assert row.encrypted_transcript is None


@pytest.mark.asyncio
async def test_tenant_cannot_retry_another_tenants_queued_job(persisted):
    factory, engine = persisted
    now = datetime.now(UTC)
    with sessionmaker(engine).begin() as db:
        db.add(
            PostCallJobRow(
                session_id="job-b",
                tenant_id="tenant-b",
                provider="vobiz",
                channel="phone",
                direction="outbound",
                ended_at=now,
                encrypted_payload="encrypted",
                status="failed",
                attempts=2,
                created_at=now,
                updated_at=now,
            )
        )
    replay = SqlPostCallReplayService.__new__(SqlPostCallReplayService)
    replay._sessions = factory

    with pytest.raises(ReplayNotFound):
        await replay.retry_analysis("tenant-a", "job-b")
    with sessionmaker(engine)() as db:
        job = db.get(PostCallJobRow, "job-b")
        assert job.tenant_id == "tenant-b"
        assert job.status == "failed"
        assert job.attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workflow_mode",
    [
        "none",
        "success",
        "failure",
        "attendance_success",
        "attendance_unclear",
        "attendance_incomplete",
        "attendance_failure",
    ],
)
async def test_api_result_and_webhook_shapes_are_additive_and_exact(
    persisted, monkeypatch, workflow_mode
):
    factory, engine = persisted
    key = Fernet.generate_key().decode("ascii")
    fernet = Fernet(key.encode("ascii"))
    queue = _new_queue(factory, key)
    session_id = f"session-{workflow_mode}"
    is_attendance = workflow_mode.startswith("attendance_")
    workflow_id = (
        None
        if workflow_mode == "none"
        else "attendance"
        if is_attendance
        else "test.persist"
    )
    call = _call(session_id, workflow_id=workflow_id)
    if is_attendance:
        call = replace(
            call,
            transcript=_attendance_transcript(),
            workflow_context={
                "caregiver_identity": "Kumar",
                "patient_identity": "Meena",
                "patient_address": "Anna Nagar",
                "attendance_type": "check_in",
            },
        )
    assert await queue.enqueue(call)
    now = datetime.now(UTC)
    with sessionmaker(engine).begin() as db:
        db.add(
            PostCallWebhookConfigRow(
                tenant_id=call.tenant_id,
                target_url="https://hooks.example.test/callback",
                encrypted_secret=fernet.encrypt(Fernet.generate_key()).decode("ascii"),
                created_at=now,
                updated_at=now,
            )
        )

    registry = PostCallWorkflowRegistry()
    model = _Model()
    if workflow_mode == "success":
        class _SuccessfulWorkflow:
            workflow_id = "test.persist"

            async def process(self, _call, _result):
                return {"decision": "accepted"}

        registry.register(_SuccessfulWorkflow())
    elif workflow_mode == "failure":
        registry.register(_FailingMutatingWorkflow())
    elif is_attendance:
        extraction = _attendance_extraction()
        if workflow_mode == "attendance_unclear":
            extraction["transcript_status"] = "unclear"
        elif workflow_mode == "attendance_incomplete":
            extraction["extracted_data"]["attendance_time"] = None
        model = _Model(
            [
                _Model.response,
                RuntimeError("attendance extractor unavailable")
                if workflow_mode == "attendance_failure"
                else extraction,
            ]
        )
        registry.register(AttendanceWorkflow(model))
    agent = LangChainPostCallAgent(model, workflow_registry=registry)
    assert await queue.process_job(session_id, agent)
    status = await queue.get_status(call.tenant_id, session_id)
    expected_result = {
        "summary": None,
        "disposition": None,
        "extracted_data": {},
        "action_items": [],
        "structured_output": {
            "ticket": "T-7",
            "nested": {"value": "caller result"},
        },
        "transcript_status": "clear",
        "needs_audio_review": False,
    }
    if workflow_mode == "success":
        expected_result["workflow_result"] = {
            "version": 1,
            "workflow_id": "test.persist",
            "status": "completed",
            "data": {"decision": "accepted"},
        }
    elif workflow_mode == "failure":
        expected_result["workflow_result"] = {
            "version": 1,
            "workflow_id": "test.persist",
            "status": "failed",
            "error_code": "workflow_execution_failed",
        }
    elif workflow_mode == "attendance_success":
        validation = _expected_attendance_validation()
        expected_result["workflow_result"] = {
            "version": 1,
            "workflow_id": "attendance",
            "status": "completed",
            "data": validation,
        }
        expected_result["attendance_validation"] = validation
    elif workflow_mode == "attendance_failure":
        expected_result["workflow_result"] = {
            "version": 1,
            "workflow_id": "attendance",
            "status": "failed",
            "error_code": "workflow_execution_failed",
        }
        expected_result["attendance_validation"] = _failed_attendance_validation()
    elif workflow_mode in {"attendance_unclear", "attendance_incomplete"}:
        extraction = _attendance_extraction()
        if workflow_mode == "attendance_unclear":
            extraction_status = "unclear"
        else:
            extraction_status = "clear"
            extraction["extracted_data"]["attendance_time"] = None
        validation = validate_attendance(
            extracted=extraction["extracted_data"],
            transcript=_attendance_transcript(),
            expected=call.workflow_context,
            live_claim=call.end_call_claim,
            tool_events=call.tool_events,
            extraction_status=extraction_status,
        )
        expected_result["workflow_result"] = {
            "version": 1,
            "workflow_id": "attendance",
            "status": "completed",
            "data": validation,
        }
        expected_result["attendance_validation"] = validation
    assert status["result"] == expected_result

    async def resolve(_url):
        return urllib.parse.urlparse("https://hooks.example.test/callback"), "93.184.216.34"

    monkeypatch.setattr(queue_module, "_resolve_public_webhook_target", resolve)
    monkeypatch.setattr(queue_module.httpx, "AsyncClient", _HttpClient)
    _HttpClient.sent = []
    assert await queue.process_delivery_one()
    sent_request, is_stream = _HttpClient.sent[-1]
    assert is_stream is True
    with sessionmaker(engine)() as db:
        ended_at = db.get(PostCallJobRow, session_id).ended_at.isoformat()
    assert json.loads(sent_request.content) == {
        "event": "call.analysis.completed",
        "event_id": f"post_call.analysis.completed:{session_id}",
        "call": {
            "session_id": session_id,
            "provider": "vobiz",
            "channel": "phone",
            "direction": "outbound",
            "started_at": None,
            "ended_at": ended_at,
        },
        "post_call": {"time_zone": "UTC", "time_format": "%Y-%m-%dT%H:%M:%S%z"},
        "result": expected_result,
    }


@pytest.mark.asyncio
async def test_legacy_queue_recovers_only_tenant_scoped_persisted_workflow(persisted):
    factory, engine = persisted
    key = Fernet.generate_key().decode("ascii")
    fernet = Fernet(key.encode("ascii"))
    queue = _new_queue(factory, key)
    call = replace(
        _call("legacy-recovery-session", workflow_id=None),
        transcript=_attendance_transcript(),
        workflow_context=None,
    )
    assert await queue.enqueue(call)
    with sessionmaker(engine).begin() as db:
        row = db.get(PostCallJobRow, call.session_id)
        payload = json.loads(fernet.decrypt(row.encrypted_payload.encode("ascii")))
        payload.pop("workflow_id", None)
        payload.pop("workflow_context", None)
        payload["expected_assignment"] = {
            "caregiver_identity": "Kumar",
            "patient_identity": "Meena",
            "patient_address": "Anna Nagar",
            "attendance_type": "check_in",
        }
        row.encrypted_payload = fernet.encrypt(json.dumps(payload).encode()).decode("ascii")

    async def resolver(tenant_id, session_id):
        assert (tenant_id, session_id) == (call.tenant_id, call.session_id)
        return SimpleNamespace(
            tenant_id=call.tenant_id,
            session_id=call.session_id,
            post_call_workflow_id="attendance",
            post_call_workflow_context=None,
        )

    queue.workflow_config_resolver = resolver
    model = _Model([_Model.response, _attendance_extraction()])
    registry = PostCallWorkflowRegistry()
    registry.register(AttendanceWorkflow(model))
    assert await queue.process_job(
        call.session_id, LangChainPostCallAgent(model, workflow_registry=registry)
    )
    status = await queue.get_status(call.tenant_id, call.session_id)
    assert status["result"]["workflow_result"]["data"]["outcome"] == "verified"
    assert status["result"]["attendance_validation"]["outcome"] == "verified"


@pytest.mark.asyncio
async def test_legacy_attendance_without_authoritative_config_is_flagged_for_review(persisted):
    factory, engine = persisted
    key = Fernet.generate_key().decode("ascii")
    fernet = Fernet(key.encode("ascii"))
    queue = _new_queue(factory, key)
    call = _call("legacy-unresolved-session", workflow_id=None)
    assert await queue.enqueue(call)
    with sessionmaker(engine).begin() as db:
        row = db.get(PostCallJobRow, call.session_id)
        payload = json.loads(fernet.decrypt(row.encrypted_payload.encode("ascii")))
        payload.pop("workflow_id", None)
        payload.pop("workflow_context", None)
        payload["expected_assignment"] = {"patient_name": "legacy"}
        row.encrypted_payload = fernet.encrypt(json.dumps(payload).encode()).decode("ascii")

    async def unavailable_resolver(_tenant_id, _session_id):
        return None

    queue.workflow_config_resolver = unavailable_resolver
    registry = PostCallWorkflowRegistry()
    registry.register(AttendanceWorkflow(_Model()))
    agent = LangChainPostCallAgent(_Model(), workflow_registry=registry)
    assert await queue.process_job(call.session_id, agent)
    status = await queue.get_status(call.tenant_id, call.session_id)
    assert status["result"]["structured_output"] == {
        "ticket": "T-7",
        "nested": {"value": "caller result"},
    }
    validation = status["result"]["attendance_validation"]
    assert validation["outcome"] == "unclear"
    assert validation["requires_manual_review"] is True
    assert validation["validation_errors"] == ["workflow_identity_unverified"]


@pytest.mark.asyncio
async def test_legacy_attendance_without_assignment_but_with_persisted_workflow_is_manual_review(
    persisted,
):
    factory, engine = persisted
    key = Fernet.generate_key().decode("ascii")
    fernet = Fernet(key.encode("ascii"))
    queue = _new_queue(factory, key)
    call = replace(
        _call("legacy-attendance-no-assignment", workflow_id=None),
        transcript=_attendance_transcript(),
        workflow_context=None,
    )
    assert await queue.enqueue(call)
    with sessionmaker(engine).begin() as db:
        row = db.get(PostCallJobRow, call.session_id)
        payload = json.loads(fernet.decrypt(row.encrypted_payload.encode("ascii")))
        payload.pop("workflow_id", None)
        payload.pop("workflow_context", None)
        payload.pop("expected_assignment", None)
        row.encrypted_payload = fernet.encrypt(json.dumps(payload).encode()).decode("ascii")

    async def persisted_attendance_config(tenant_id, session_id):
        return SimpleNamespace(
            tenant_id=tenant_id,
            session_id=session_id,
            post_call_workflow_id="attendance",
            post_call_workflow_context=None,
        )

    queue.workflow_config_resolver = persisted_attendance_config
    model = _Model([_Model.response])
    registry = PostCallWorkflowRegistry()
    registry.register(AttendanceWorkflow(model))
    assert await queue.process_job(
        call.session_id, LangChainPostCallAgent(model, workflow_registry=registry)
    )
    status = await queue.get_status(call.tenant_id, call.session_id)
    validation = status["result"]["workflow_result"]["data"]
    assert validation["outcome"] == "incomplete"
    assert validation["requires_manual_review"] is True
    assert "mandatory_assignment_configuration_missing" in validation["validation_errors"]
    assert status["result"]["attendance_validation"] == validation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_id,legacy_context",
    [
        ("legacy-non-attendance", "Summarize the customer support request."),
        ("legacy-ambiguous", "Caregiver check-in; confirm the caller arrived."),
    ],
)
async def test_legacy_unproven_workflow_stays_generic(persisted, session_id, legacy_context):
    factory, engine = persisted
    key = Fernet.generate_key().decode("ascii")
    fernet = Fernet(key.encode("ascii"))
    queue = _new_queue(factory, key)
    call = replace(
        _call(session_id, workflow_id=None),
        post_call_context=legacy_context,
        workflow_context=None,
    )
    assert await queue.enqueue(call)
    with sessionmaker(engine).begin() as db:
        row = db.get(PostCallJobRow, call.session_id)
        payload = json.loads(fernet.decrypt(row.encrypted_payload.encode("ascii")))
        payload.pop("workflow_id", None)
        payload.pop("workflow_context", None)
        payload.pop("expected_assignment", None)
        row.encrypted_payload = fernet.encrypt(json.dumps(payload).encode()).decode("ascii")

    async def no_authoritative_config(_tenant_id, _session_id):
        return None

    queue.workflow_config_resolver = no_authoritative_config
    registry = PostCallWorkflowRegistry()
    registry.register(AttendanceWorkflow(_Model()))
    assert await queue.process_job(
        call.session_id, LangChainPostCallAgent(_Model(), workflow_registry=registry)
    )
    status = await queue.get_status(call.tenant_id, call.session_id)
    assert status["result"]["structured_output"] == {
        "ticket": "T-7",
        "nested": {"value": "caller result"},
    }
    assert "workflow_result" not in status["result"]
    assert "attendance_validation" not in status["result"]


@pytest.mark.asyncio
async def test_legacy_queue_rejects_persisted_configuration_for_another_session(persisted):
    factory, engine = persisted
    key = Fernet.generate_key().decode("ascii")
    fernet = Fernet(key.encode("ascii"))
    queue = _new_queue(factory, key)
    call = _call("legacy-cross-session", workflow_id=None)
    assert await queue.enqueue(call)
    with sessionmaker(engine).begin() as db:
        row = db.get(PostCallJobRow, call.session_id)
        payload = json.loads(fernet.decrypt(row.encrypted_payload.encode("ascii")))
        payload.pop("workflow_id", None)
        payload.pop("workflow_context", None)
        row.encrypted_payload = fernet.encrypt(json.dumps(payload).encode()).decode("ascii")

    async def other_session_config(tenant_id, _session_id):
        return SimpleNamespace(
            tenant_id=tenant_id,
            session_id="different-session",
            post_call_workflow_id="attendance",
            post_call_workflow_context={"leaked": "context"},
        )

    queue.workflow_config_resolver = other_session_config
    seen = {}

    class Agent:
        async def process(self, recovered):
            seen["workflow_id"] = recovered.workflow_id
            seen["context"] = recovered.workflow_context
            return PostCallResult(structured_output={"generic": True})

    assert await queue.process_job(call.session_id, Agent())
    assert seen == {"workflow_id": None, "context": None}



@pytest.mark.asyncio
async def test_generic_evidence_and_result_survive_queue_retry(persisted):
    factory, engine = persisted
    key = Fernet.generate_key().decode("ascii")
    fernet = Fernet(key.encode("ascii"))
    queue = _new_queue(factory, key)
    call = _call("retry-session")
    assert await queue.enqueue(call)

    registry = PostCallWorkflowRegistry()
    registry.register(_FailingMutatingWorkflow())
    agent = LangChainPostCallAgent(_Model(), workflow_registry=registry)

    class _FailOnce:
        def __init__(self):
            self.calls = []

        async def process(self, completed_call):
            self.calls.append(completed_call)
            if len(self.calls) == 1:
                raise RuntimeError("transient worker failure")
            return await agent.process(completed_call)

    flaky_agent = _FailOnce()
    assert await queue.process_job(call.session_id, flaky_agent)
    with sessionmaker(engine).begin() as db:
        job = db.get(PostCallJobRow, call.session_id)
        assert job.status == "queued"
        assert job.attempts == 1
        job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)

    assert await queue.process_job(call.session_id, flaky_agent)
    assert len(flaky_agent.calls) == 2
    for received in flaky_agent.calls:
        assert received.transcript == "Caller: ticket T-7"
        assert received.workflow_context == {"session_marker": call.session_id}
        assert received.end_call_claim == {"reason": "completed"}
        assert received.tool_events == (
            {"tool": "record_caller_identity", "result": "captured"},
        )

    status = await queue.get_status(call.tenant_id, call.session_id)
    assert status["status"] == "completed"
    assert status["result"]["structured_output"] == {
        "ticket": "T-7",
        "nested": {"value": "caller result"},
    }
    assert status["result"]["workflow_result"]["status"] == "failed"
    with sessionmaker(engine)() as db:
        row = db.get(PostCallJobRow, call.session_id)
        payload = json.loads(fernet.decrypt(row.encrypted_payload.encode("ascii")))
        assert payload["transcript"] == call.transcript
        assert payload["workflow_context"] == call.workflow_context
        assert payload["end_call_claim"] == call.end_call_claim
        assert payload["tool_events"] == list(call.tool_events)


@pytest.mark.asyncio
async def test_workflow_failure_persists_generic_call_result(persisted):
    factory, _engine = persisted
    key = Fernet.generate_key().decode("ascii")
    queue = _new_queue(factory, key)
    call = CompletedCall(
        session_id="generic-result-session",
        tenant_id="tenant-b",
        provider="vobiz",
        channel="phone",
        direction="outbound",
        transcript="Caller: please create ticket T-9",
        workflow_id="test.generic-failure",
    )
    assert await queue.enqueue(call)

    class _GenericModel(_Model):
        response = {
            "transcript_status": "clear",
            "summary": "Caller requested ticket T-9.",
            "disposition": "completed",
            "extracted_data": {"ticket": "T-9"},
            "action_items": ["Create ticket T-9"],
            "needs_audio_review": False,
        }

    registry = PostCallWorkflowRegistry()
    registry.register(_RaisingWorkflow())
    agent = LangChainPostCallAgent(_GenericModel(), workflow_registry=registry)
    assert await queue.process_job(call.session_id, agent)
    result = (await queue.get_status(call.tenant_id, call.session_id))["result"]

    assert result["summary"] == "Caller requested ticket T-9."
    assert result["disposition"] == "completed"
    assert result["extracted_data"] == {"ticket": "T-9"}
    assert result["action_items"] == ["Create ticket T-9"]
    assert result["workflow_result"] == {
        "version": 1,
        "workflow_id": "test.generic-failure",
        "status": "failed",
        "error_code": "workflow_execution_failed",
    }
