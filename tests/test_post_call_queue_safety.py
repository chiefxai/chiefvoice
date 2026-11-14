from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from chiefvoice.agents.post_call.queue import SqlPostCallQueue
from chiefvoice.agents.post_call.replay import ReplayConflict, SqlPostCallReplayService


def _sql(statement) -> tuple[str, dict]:
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    return str(compiled), compiled.params


@pytest.mark.asyncio
async def test_retention_query_only_selects_terminal_jobs_without_pending_webhooks():
    class _Result:
        def all(self):
            return []

    class _Session:
        statement = None

        async def scalars(self, statement):
            self.statement = statement
            return _Result()

        async def execute(self, statement):
            return None

    session = _Session()

    class _Sessions:
        @asynccontextmanager
        async def begin(self):
            yield session

    queue = SqlPostCallQueue.__new__(SqlPostCallQueue)
    queue._sessions = _Sessions()

    await queue.purge_expired(
        "tenant-a", datetime.now(UTC) - timedelta(days=30), batch_size=100
    )

    sql, _ = _sql(session.statement)
    assert "post_call_jobs.status IN ('completed', 'failed')" in sql
    assert "NOT (EXISTS" in sql
    assert "post_call_webhook_deliveries.status IN ('queued', 'processing')" in sql


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _ReplaySession:
    def __init__(self, row):
        self.row = row
        self.statements = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.row


class _ReplaySessions:
    def __init__(self, session):
        self.session = session

    def begin(self):
        return _SessionContext(self.session)


@pytest.mark.asyncio
async def test_analysis_replay_is_tenant_scoped_and_only_resets_failed_jobs():
    class _Job:
        status = "failed"
        attempts = 5
        next_attempt_at = None
        lease_expires_at = datetime.now(UTC)
        last_error_type = "RuntimeError"
        last_dispatch_error_type = "TimeoutError"
        last_dispatched_at = datetime.now(UTC)
        updated_at = None

    session = _ReplaySession(_Job())
    service = SqlPostCallReplayService.__new__(SqlPostCallReplayService)
    service._sessions = _ReplaySessions(session)

    result = await service.retry_analysis("tenant-a", "call-1")

    sql, _ = _sql(session.statements[0])
    assert "post_call_jobs.tenant_id = 'tenant-a'" in sql
    assert "post_call_jobs.session_id = 'call-1'" in sql
    assert "FOR UPDATE" in sql
    assert result == {"session_id": "call-1", "status": "queued"}
    assert session.row.status == "queued"
    assert session.row.attempts == 0
    assert session.row.last_error_type is None
    assert session.row.last_dispatched_at is None


@pytest.mark.asyncio
async def test_analysis_replay_refuses_a_nonfailed_job_without_mutating_it():
    class _Job:
        status = "processing"
        attempts = 2

    session = _ReplaySession(_Job())
    service = SqlPostCallReplayService.__new__(SqlPostCallReplayService)
    service._sessions = _ReplaySessions(session)

    with pytest.raises(ReplayConflict):
        await service.retry_analysis("tenant-a", "call-1")
    assert session.row.status == "processing"
    assert session.row.attempts == 2


@pytest.mark.asyncio
async def test_webhook_replay_uses_current_tenant_config_and_preserves_event_id():
    class _Job:
        status = "completed"
        encrypted_result = "encrypted-result"

    class _Delivery:
        event_id = "post_call.analysis.completed:call-1"
        status = "failed"
        attempts = 5
        target_url = ""
        encrypted_secret = ""
        next_attempt_at = None
        lease_expires_at = None
        last_status_code = 500
        last_error_type = "webhook_http_500"
        delivered_at = None
        updated_at = None

    class _Session(_ReplaySession):
        def __init__(self, config=True):
            self.job = _Job()
            self.delivery = _Delivery()
            self.rows = [self.job, self.delivery]
            self.config = config
            super().__init__(None)

        async def scalar(self, statement):
            self.statements.append(statement)
            return self.rows.pop(0)

        async def get(self, model, tenant_id):
            if not self.config:
                return None
            return SimpleNamespace(
                target_url="https://crm.example.test/callback",
                encrypted_secret="tenant-current-secret",
            )

    session = _Session()
    service = SqlPostCallReplayService.__new__(SqlPostCallReplayService)
    service._sessions = _ReplaySessions(session)

    result = await service.retry_webhook("tenant-a", "call-1")

    first_query, _ = _sql(session.statements[0])
    second_query, _ = _sql(session.statements[1])
    assert "post_call_jobs.tenant_id = 'tenant-a'" in first_query
    assert "post_call_webhook_deliveries.tenant_id = 'tenant-a'" in second_query
    assert result == {"session_id": "call-1", "status": "queued"}
    assert session.delivery.target_url == "https://crm.example.test/callback"
    assert session.delivery.encrypted_secret == "tenant-current-secret"
    assert session.delivery.event_id == "post_call.analysis.completed:call-1"
    assert session.delivery.attempts == 0


@pytest.mark.asyncio
async def test_webhook_replay_requires_a_current_tenant_callback_config():
    class _Job:
        status = "completed"
        encrypted_result = "encrypted-result"

    class _Delivery:
        event_id = "stable-event"
        status = "failed"
        attempts = 5

    class _Session(_ReplaySession):
        def __init__(self):
            self.rows = [_Job(), _Delivery()]
            self.delivery = self.rows[1]
            super().__init__(None)

        async def scalar(self, statement):
            self.statements.append(statement)
            return self.rows.pop(0)

        async def get(self, model, tenant_id):
            return None

    session = _Session()
    service = SqlPostCallReplayService.__new__(SqlPostCallReplayService)
    service._sessions = _ReplaySessions(session)

    with pytest.raises(ReplayConflict, match="Configure a post-call webhook"):
        await service.retry_webhook("tenant-a", "call-1")
    assert session.delivery.status == "failed"
    assert session.delivery.event_id == "stable-event"
