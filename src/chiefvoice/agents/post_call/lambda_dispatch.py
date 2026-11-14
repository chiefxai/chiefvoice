from __future__ import annotations

import asyncio
import json


class LambdaAsyncInvoker:
    """AWS Lambda adapter; sends only an idempotent Postgres job identifier."""

    def __init__(self, function_name: str, region_name: str | None = None):
        if not function_name.strip():
            raise ValueError("A Lambda function name is required")
        import boto3

        self._client = boto3.client("lambda", region_name=region_name)
        self._function_name = function_name

    async def invoke(self, job_id: str) -> None:
        response = await asyncio.to_thread(
            self._client.invoke,
            FunctionName=self._function_name,
            InvocationType="Event",
            Payload=json.dumps({"schema_version": 1, "job_id": job_id}).encode("utf-8"),
        )
        if response.get("StatusCode") != 202:
            raise RuntimeError("Lambda did not accept the asynchronous invocation")
