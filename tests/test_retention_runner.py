import asyncio
from datetime import UTC, datetime, timedelta

from chiefvoice.data_lifecycle.retention import (
    RetentionClass,
    RetentionRunner,
    StaticRetentionPolicyDirectory,
)


class _Target:
    data_class = RetentionClass.RECORDING

    def __init__(self):
        self.calls = []

    async def purge_expired(self, tenant_id, cutoff, batch_size):
        self.calls.append((tenant_id, cutoff, batch_size))
        return 2


def test_runner_applies_explicit_tenant_retention_policy_in_batches():
    directory = StaticRetentionPolicyDirectory.from_json(
        '[{"tenant_id":"tenant-a","data_class":"recording",'
        '"retain_for_seconds":86400}]'
    )
    target = _Target()
    runner = RetentionRunner(directory, [target], batch_size=25)
    now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)

    result = asyncio.run(runner.run_once(now))

    assert result == {"tenant-a:recording": 2}
    assert target.calls == [("tenant-a", now - timedelta(days=1), 25)]


def test_legal_hold_disables_retention_for_tenant_data_class():
    directory = StaticRetentionPolicyDirectory.from_json(
        '[{"tenant_id":"tenant-a","data_class":"recording",'
        '"retain_for_seconds":86400,"legal_hold":true}]'
    )
    target = _Target()
    runner = RetentionRunner(directory, [target])

    assert asyncio.run(runner.run_once(datetime.now(UTC))) == {}
    assert target.calls == []
