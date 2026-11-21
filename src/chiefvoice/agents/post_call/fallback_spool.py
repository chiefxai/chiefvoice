"""Encrypted S3-compatible fallback spool for completed calls that cannot reach Postgres."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import asdict
from typing import Protocol

from cryptography.fernet import Fernet

from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.storage.s3 import create_s3_compatible_client, object_key

logger = logging.getLogger(__name__)
MAX_SPOOLED_JOB_BYTES = 8 * 1024 * 1024


class PostCallFallbackSpool(Protocol):
    async def store(self, call: CompletedCall) -> None: ...

    async def list_pending(self, limit: int = 100) -> list[CompletedCall]: ...

    async def delete(self, session_id: str) -> None: ...


class S3PostCallFallbackSpool:
    """Store encrypted call payloads outside Postgres for later idempotent reconciliation.

    The object key is a stable hash of the call session ID. Tenant and caller data exist only
    inside the Fernet-encrypted object body. Objects must not expire automatically while they
    remain pending; reconciliation deletes them after queue insertion or duplicate confirmation.
    """

    def __init__(
        self,
        bucket: str,
        encryption_key: str,
        *,
        client=None,
        prefix: str = "post-call-recovery",
        max_object_bytes: int = MAX_SPOOLED_JOB_BYTES,
    ):
        if not bucket.strip():
            raise ValueError("A recovery spool bucket is required")
        if max_object_bytes < 1:
            raise ValueError("Recovery spool object limit must be positive")
        if not prefix.strip("/"):
            raise ValueError("Recovery spool prefix must not be empty")
        self._bucket = bucket
        self._fernet = Fernet(encryption_key.encode("ascii"))
        self._client = client or create_s3_compatible_client()
        self._prefix = prefix.strip("/")
        self._max_object_bytes = max_object_bytes

    async def store(self, call: CompletedCall) -> None:
        if (
            not call.session_id
            or not call.tenant_id
            or not (call.transcript and call.transcript.strip()) and not call.recording_uri
        ):
            raise ValueError("A completed call with tenant and transcript or recording is required")
        payload = json.dumps(asdict(call), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        encrypted = self._fernet.encrypt(payload)
        if len(encrypted) > self._max_object_bytes:
            raise ValueError("Completed call exceeds the configured recovery spool object limit")
        key = self._object_key(call.session_id)
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=encrypted,
            ContentType="application/octet-stream",
            Metadata={"format": "chiefvoice-post-call-v1"},
        )
        logger.warning(
            "post_call_job_staged_for_recovery",
            extra={"session_id": call.session_id, "event": "post_call_fallback_saved"},
        )

    async def check_storage(self) -> None:
        await asyncio.to_thread(
            self._client.list_objects_v2,
            Bucket=self._bucket,
            Prefix=object_key(self._prefix) + "/",
            MaxKeys=1,
        )

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await asyncio.to_thread(close)

    async def list_pending(self, limit: int = 100) -> list[CompletedCall]:
        """Read up to `limit` staged records for a worker to enqueue into Postgres."""
        if limit < 1:
            raise ValueError("Recovery spool batch size must be positive")
        calls: list[CompletedCall] = []
        request = {
            "Bucket": self._bucket,
            "Prefix": object_key(self._prefix) + "/",
            "MaxKeys": 1000,
        }
        while len(calls) < limit:
            response = await asyncio.to_thread(self._client.list_objects_v2, **request)
            for item in response.get("Contents", []):
                key = item.get("Key")
                if not isinstance(key, str):
                    continue
                try:
                    obj = await asyncio.to_thread(
                        self._client.get_object, Bucket=self._bucket, Key=key
                    )
                    encrypted = await asyncio.to_thread(
                        _read_bounded_body, obj["Body"], self._max_object_bytes + 1
                    )
                    if len(encrypted) > self._max_object_bytes:
                        raise ValueError("Recovery spool object exceeds configured limit")
                    payload = json.loads(self._fernet.decrypt(encrypted))
                    calls.append(_completed_call(payload))
                except Exception as exc:
                    logger.error(
                        "post_call_recovery_spool_read_failed",
                        extra={"error_type": type(exc).__name__},
                    )
                if len(calls) >= limit:
                    break
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                raise RuntimeError("S3 recovery spool listing was truncated without a continuation token")
            request["ContinuationToken"] = token
        return calls

    async def delete(self, session_id: str) -> None:
        """Remove a staged object only after queue insertion or duplicate confirmation."""
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=self._object_key(session_id),
        )

    def _object_key(self, session_id: str) -> str:
        session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return object_key(self._prefix, f"{session_digest}.fernet")


def _completed_call(payload: dict) -> CompletedCall:
    required = {"session_id", "tenant_id", "provider", "channel", "direction"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ValueError("Malformed post-call recovery payload")
    return CompletedCall(
        session_id=payload["session_id"],
        tenant_id=payload["tenant_id"],
        provider=payload["provider"],
        channel=payload["channel"],
        direction=payload["direction"],
        live_agent_language=payload.get("live_agent_language"),
        api_key_id=payload.get("api_key_id"),
        started_at=payload.get("started_at"),
        ended_at=payload.get("ended_at"),
        transcript=payload.get("transcript"),
        recording_uri=payload.get("recording_uri"),
        post_call_context=payload.get("post_call_context"),
        output_schema=payload.get("output_schema"),
        tools=tuple(payload.get("tools", ())),
        time_zone=payload.get("time_zone", "UTC"),
        time_format=payload.get("time_format", "%Y-%m-%dT%H:%M:%S%z"),
        live_agent_context=payload.get("live_agent_context"),
        failure_webhook_url=payload.get("failure_webhook_url"),
        failure_webhook_payload=payload.get("failure_webhook_payload"),
    )


def _read_bounded_body(body, limit: int) -> bytes:
    try:
        return body.read(limit)
    finally:
        body.close()
