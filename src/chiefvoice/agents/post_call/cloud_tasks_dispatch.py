from __future__ import annotations

import asyncio
import json


class CloudTasksPostCallInvoker:
    """Enqueue an authenticated HTTP task targeting the post-call function."""

    def __init__(self, queue_path: str, function_url: str, service_account_email: str):
        if not queue_path or not function_url.startswith("https://") or not service_account_email:
            raise ValueError("Cloud Tasks queue, HTTPS function URL and service account are required")
        from google.cloud import tasks_v2  # type: ignore[import-untyped]

        self._client = tasks_v2.CloudTasksClient()
        self._queue_path = queue_path
        self._function_url = function_url
        self._service_account_email = service_account_email

    async def invoke(self, job_id: str) -> None:
        task = {
            "http_request": {
                "http_method": "POST",
                "url": self._function_url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"schema_version": 1, "job_id": job_id}).encode(),
                "oidc_token": {"service_account_email": self._service_account_email, "audience": self._function_url},
            }
        }
        await asyncio.to_thread(self._client.create_task, request={"parent": self._queue_path, "task": task})
