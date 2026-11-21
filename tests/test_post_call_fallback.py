import asyncio
from io import BytesIO

import pytest
from cryptography.fernet import Fernet

from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.agents.post_call.fallback_spool import S3PostCallFallbackSpool
from chiefvoice.agents.post_call.recovery import run_spool_reconciler


class _MemoryS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = bytes(Body)

    def list_objects_v2(self, *, Bucket, Prefix, MaxKeys, **kwargs):
        keys = [key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix)]
        return {"Contents": [{"Key": key} for key in keys[:MaxKeys]], "IsTruncated": False}

    def get_object(self, *, Bucket, Key):
        return {"Body": BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def _call() -> CompletedCall:
    return CompletedCall(
        session_id="session-sensitive-123",
        tenant_id="tenant-secret",
        provider="vobiz",
        channel="phone",
        direction="outbound",
        transcript="private patient transcript",
        post_call_context="extract appointment confirmation",
        tools=("time",),
        live_agent_context={"name": "Patient"},
        workflow_id="example.workflow",
        workflow_context={"session_marker": "recovery-context"},
        end_call_claim={"reason": "caller-confirmed"},
        tool_events=({"tool": "capture", "value": "verified event"},),
    )


@pytest.mark.asyncio
async def test_s3_fallback_spool_encrypts_payload_and_round_trips_call():
    key = Fernet.generate_key().decode("ascii")
    client = _MemoryS3()
    spool = S3PostCallFallbackSpool("recovery-bucket", key, client=client)
    call = _call()

    await spool.store(call)

    (bucket, object_key), encrypted = next(iter(client.objects.items()))
    assert bucket == "recovery-bucket"
    assert "session-sensitive-123" not in object_key
    assert "session-sensitive-123" not in encrypted.decode("latin1")
    assert "private patient transcript" not in encrypted.decode("latin1")
    recovered = await spool.list_pending()
    assert recovered == [call]
    assert recovered[0].workflow_context == {"session_marker": "recovery-context"}
    assert recovered[0].end_call_claim == {"reason": "caller-confirmed"}
    assert recovered[0].tool_events == ({"tool": "capture", "value": "verified event"},)

    await spool.delete(call.session_id)
    assert client.objects == {}


@pytest.mark.asyncio
async def test_spool_reconciler_deletes_only_after_queue_accepts_or_confirms_duplicate():
    call = _call()

    class Spool:
        def __init__(self):
            self.deleted = []
            self.done = asyncio.Event()

        async def list_pending(self, limit=100):
            return [call]

        async def delete(self, session_id):
            self.deleted.append(session_id)
            self.done.set()

    class Queue:
        async def enqueue(self, completed_call):
            assert completed_call == call
            return False  # Existing session ID is already in the Postgres outbox.

    spool = Spool()
    task = asyncio.create_task(run_spool_reconciler(spool, Queue(), poll_interval=3600))
    try:
        await asyncio.wait_for(spool.done.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert spool.deleted == [call.session_id]


@pytest.mark.asyncio
async def test_spool_reconciler_retains_object_when_postgres_enqueue_fails():
    call = _call()

    class Spool:
        def __init__(self):
            self.deleted = []
            self.scanned = asyncio.Event()

        async def list_pending(self, limit=100):
            self.scanned.set()
            return [call]

        async def delete(self, session_id):
            self.deleted.append(session_id)

    class Queue:
        async def enqueue(self, completed_call):
            spool.enqueue_attempted.set()
            raise ConnectionError("database unavailable")

    spool = Spool()
    spool.enqueue_attempted = asyncio.Event()
    task = asyncio.create_task(run_spool_reconciler(spool, Queue(), poll_interval=3600))
    try:
        await asyncio.wait_for(spool.enqueue_attempted.wait(), timeout=1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert spool.deleted == []
