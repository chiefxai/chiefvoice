from types import SimpleNamespace
from pathlib import Path

import pytest

from chiefvoice.agents.post_call.gcp_function_dispatch import GoogleCloudFunctionInvoker


def test_cloud_build_deploys_function_with_direct_vpc_egress():
    cloudbuild = (Path(__file__).resolve().parents[1] / "cloudbuild.yaml").read_text(
        encoding="utf-8"
    )
    assert "gcr.io/google.com/cloudsdktool/cloud-sdk:558.0.0-slim" in cloudbuild
    assert "--network=${_FUNCTION_NETWORK}" in cloudbuild
    assert "--subnet=${_FUNCTION_SUBNET}" in cloudbuild
    assert "--direct-vpc-egress=private-ranges-only" in cloudbuild
    assert '--region=${_FUNCTION_REGION}' in cloudbuild
    assert 'test "${_FUNCTION_REGION}" = "asia-south1"' in cloudbuild
    assert "--vpc-connector" not in cloudbuild
    assert "--egress-settings" not in cloudbuild


def test_gcp_function_url_requires_https_without_query_or_credentials():
    for url in (
        "http://function.example.com/post-call",
        "https://user:secret@function.example.com/post-call",
        "https://function.example.com/post-call?token=secret",
        "https://function.example.com/post-call#fragment",
    ):
        with pytest.raises(ValueError):
            GoogleCloudFunctionInvoker(url)


async def test_gcp_function_invoker_posts_job_id_with_oidc_token(monkeypatch):
    observed = {}

    class _Client:
        def __init__(self, **kwargs):
            observed["client_options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            observed.update(url=url, headers=headers, payload=json)
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(
        "chiefvoice.agents.post_call.gcp_function_dispatch.httpx.AsyncClient", _Client
    )
    monkeypatch.setattr(
        GoogleCloudFunctionInvoker,
        "_fetch_id_token",
        staticmethod(lambda audience: f"token-for:{audience}"),
    )
    invoker = GoogleCloudFunctionInvoker("https://us-central1-project.cloudfunctions.net/post-call")

    await invoker.invoke("job-123")

    assert observed["url"] == "https://us-central1-project.cloudfunctions.net/post-call"
    assert observed["headers"]["Authorization"].startswith("Bearer token-for:")
    assert observed["payload"] == {"schema_version": 1, "job_id": "job-123"}
    assert observed["client_options"]["follow_redirects"] is False


async def test_gcp_function_invoker_raises_when_function_rejects_job(monkeypatch):
    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=503)

    monkeypatch.setattr(
        "chiefvoice.agents.post_call.gcp_function_dispatch.httpx.AsyncClient", _Client
    )
    monkeypatch.setattr(
        GoogleCloudFunctionInvoker,
        "_fetch_id_token",
        staticmethod(lambda audience: "test-token"),
    )
    invoker = GoogleCloudFunctionInvoker("https://function.example.com/post-call")

    with pytest.raises(RuntimeError, match="HTTP 503"):
        await invoker.invoke("job-123")
