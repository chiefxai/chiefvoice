from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx


class ChiefvoiceClient:
    """Small async client for outbound calls, post-call polling, and callback setup.

    The client never retries call creation automatically: a network timeout can happen
    after a provider has accepted the call, and this API does not yet accept an
    idempotency key for call-start requests.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout_seconds: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key.strip():
            raise ValueError("api_key is required")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_seconds, connect=5),
            transport=transport,
        )

    async def start_outbound(self, request: Mapping[str, Any]) -> dict[str, Any]:
        response = await self._client.post("/v1/calls/outbound", json=dict(request))
        response.raise_for_status()
        return response.json()

    async def get_post_call(self, session_id: str) -> dict[str, Any]:
        response = await self._client.get(
            f"/v1/calls/{quote(session_id, safe='')}/post-call",
        )
        response.raise_for_status()
        return response.json()

    async def wait_for_post_call(
        self,
        session_id: str,
        *,
        timeout_seconds: float = 900,
        poll_interval_seconds: float = 2,
    ) -> dict[str, Any]:
        if timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("Polling timeout and interval must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            result = await self.get_post_call(session_id)
            if result.get("status") in {"completed", "failed"}:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Post-call job {session_id} did not finish in time")
            await asyncio.sleep(min(poll_interval_seconds, remaining))

    async def configure_post_call_webhook(
        self,
        target_url: str,
        *,
        failure_webhook_url: str | None = None,
        failure_webhook_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"target_url": target_url}
        if failure_webhook_url is not None:
            body["failure_webhook_url"] = failure_webhook_url
        if failure_webhook_payload is not None:
            body["failure_webhook_payload"] = dict(failure_webhook_payload)
        response = await self._client.put(
            "/v1/webhooks/post-call",
            json=body,
        )
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> ChiefvoiceClient:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()
