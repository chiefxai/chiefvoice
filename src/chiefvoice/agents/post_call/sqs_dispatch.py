from __future__ import annotations

import asyncio
import json


class SqsPostCallInvoker:
    """Publish durable job identifiers to an AWS SQS queue."""

    def __init__(self, queue_url: str, region_name: str | None = None):
        if not queue_url.strip():
            raise ValueError("POST_CALL_SQS_QUEUE_URL is required")
        import boto3  # type: ignore[import-untyped]

        self._client = boto3.client("sqs", region_name=region_name)
        self._queue_url = queue_url

    async def invoke(self, job_id: str) -> None:
        await asyncio.to_thread(
            self._client.send_message,
            QueueUrl=self._queue_url,
            MessageBody=json.dumps({"schema_version": 1, "job_id": job_id}),
        )
