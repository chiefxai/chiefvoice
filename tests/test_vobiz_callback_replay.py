import base64
import hashlib
import hmac
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException, Request

from chiefvoice.channels.vobiz.adapter import VobizAdapter


class _IntegrationStore:
    async def get_for_inbound_number(self, provider, number):
        assert provider == "vobiz"
        assert number == "+15555550123"
        return SimpleNamespace(
            tenant_id="tenant-a",
            credentials={"auth_token": "account-token"},
            settings={},
        )


class _NonceStore:
    def __init__(self):
        self._nonces = set()

    async def consume(self, tenant_id, nonce):
        key = (tenant_id, nonce)
        if key in self._nonces:
            return False
        self._nonces.add(key)
        return True


def _request(settings):
    nonce = "provider-nonce-123"
    message = f"{settings.vobiz_public_url}/webhooks/vobiz/inbound.{nonce}"
    signature = base64.b64encode(
        hmac.new(b"account-token", message.encode(), hashlib.sha256).digest()
    ).decode()
    body = urlencode(
        {"From": "+15555550000", "To": "+15555550123", "CallUUID": "call-123"}
    ).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhooks/vobiz/inbound",
        "query_string": b"key=route-token",
        "headers": [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode()),
            (b"x-vobiz-signature-v3", signature.encode()),
            (b"x-vobiz-signature-v3-nonce", nonce.encode()),
        ],
        "server": ("voice.example.test", 443),
        "scheme": "https",
        "client": ("127.0.0.1", 1234),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_vobiz_inbound_accepts_a_signed_nonce_once(monkeypatch):
    settings = SimpleNamespace(
        vobiz_public_url="https://voice.example.test",
        vobiz_inbound_route_token="route-token",
        vobiz_webhook_secret="platform-secret",
        vobiz_sample_rate=8000,
        vobiz_encoding="audio/x-mulaw",
    )
    monkeypatch.setattr("chiefvoice.channels.vobiz.adapter.settings", settings)
    nonce_store = _NonceStore()
    adapter = VobizAdapter(_IntegrationStore(), nonce_store)

    call = await adapter.parse_inbound(_request(settings))
    assert call.tenant_id == "tenant-a"
    assert call.provider_call_id == "call-123"

    with pytest.raises(HTTPException, match="Replayed Vobiz callback") as exc:
        await adapter.parse_inbound(_request(settings))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_vobiz_inbound_fails_closed_without_shared_nonce_store(monkeypatch):
    settings = SimpleNamespace(
        vobiz_public_url="https://voice.example.test",
        vobiz_inbound_route_token="route-token",
        vobiz_webhook_secret="platform-secret",
        vobiz_sample_rate=8000,
        vobiz_encoding="audio/x-mulaw",
    )
    monkeypatch.setattr("chiefvoice.channels.vobiz.adapter.settings", settings)
    adapter = VobizAdapter(_IntegrationStore())

    with pytest.raises(HTTPException) as exc:
        await adapter.parse_inbound(_request(settings))
    assert exc.value.status_code == 503
