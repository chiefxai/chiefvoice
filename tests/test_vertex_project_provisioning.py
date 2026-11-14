from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from chiefvoice.cloud_projects.store import SqlTenantVertexProjectDirectory


class _FakeSession:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def scalar(self, statement):
        return self.row

    async def get(self, model, tenant_id):
        return self.row if tenant_id == self.row.tenant_id else None


class _FakeSessions:
    def __init__(self, row):
        self.session = _FakeSession(row)

    def begin(self):
        return self.session


class _Provisioner:
    def __init__(self, error=None):
        self.error = error

    def provision(self, tenant_id, project_id, display_name):
        if self.error:
            raise self.error
        return "billingAccounts/test/budgets/test"


def _directory_and_row():
    row = SimpleNamespace(
        tenant_id="tenant-1",
        project_id="cv-project",
        display_name="Customer",
        status="pending",
        attempts=0,
        next_attempt_at=datetime.now(UTC),
        lease_expires_at=None,
        last_error_type=None,
        budget_name=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    directory = object.__new__(SqlTenantVertexProjectDirectory)
    directory._sessions = _FakeSessions(row)
    directory.max_attempts = 5
    directory.lease_duration = timedelta(minutes=15)
    return directory, row


async def test_project_provisioning_marks_project_ready_and_records_budget():
    directory, row = _directory_and_row()

    worked = await directory.provision_one(_Provisioner())

    assert worked is True
    assert row.status == "ready"
    assert row.budget_name == "billingAccounts/test/budgets/test"
    assert row.last_error_type is None


async def test_project_provisioning_failure_is_retryable():
    directory, row = _directory_and_row()

    worked = await directory.provision_one(_Provisioner(RuntimeError("provider failed")))

    assert worked is True
    assert row.status == "pending"
    assert row.last_error_type == "RuntimeError"
    assert row.next_attempt_at is not None
    assert row.lease_expires_at is None
