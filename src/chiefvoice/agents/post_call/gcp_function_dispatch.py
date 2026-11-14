from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx


class GoogleCloudFunctionInvoker:
    """Invoke an authenticated 2nd gen Cloud Run function with an OIDC ID token."""

    def __init__(self, function_url: str, *, timeout_seconds: float = 180.0):
        parsed = urlsplit(function_url.strip())
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("POST_CALL_GCP_FUNCTION_URL must be an HTTPS function URL")
        self._function_url = function_url.strip().rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds, connect=5.0)

    @staticmethod
    def _fetch_id_token(audience: str) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        return id_token.fetch_id_token(Request(), audience)

    async def invoke(self, job_id: str) -> None:
        token = await asyncio.to_thread(self._fetch_id_token, self._function_url)
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
            response = await client.post(
                self._function_url,
                headers={"Authorization": f"Bearer {token}"},
                json={"schema_version": 1, "job_id": job_id},
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"Google Cloud Function rejected the post-call job with HTTP {response.status_code}"
            )
