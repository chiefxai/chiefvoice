import json

import httpx
import pytest

from chiefvoice_sdk import ChiefvoiceClient


@pytest.mark.asyncio
async def test_sdk_starts_call_and_polls_for_post_call_result():
    statuses = iter(["queued", "completed"])
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/calls/outbound":
            assert request.headers["authorization"] == "Bearer cv_live_test"
            assert json.loads(request.content)["destination"] == "+15551234567"
            return httpx.Response(202, json={"session_id": "call-1", "status": "starting"})
        return httpx.Response(200, json={"session_id": "call-1", "status": next(statuses)})

    async with ChiefvoiceClient(
        "cv_live_test",
        base_url="https://voice.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        accepted = await client.start_outbound(
            {"provider": "vobiz", "channel": "phone", "destination": "+15551234567"}
        )
        result = await client.wait_for_post_call(
            accepted["session_id"], poll_interval_seconds=0.001
        )

    assert result["status"] == "completed"
    assert len(requests) == 3
