import base64
import hashlib
import hmac
from types import SimpleNamespace

from chiefvoice.channels.vobiz.adapter import VobizAdapter


def test_validates_vobiz_v3_signature(monkeypatch):
    monkeypatch.setattr(
        "chiefvoice.channels.vobiz.adapter.settings",
        SimpleNamespace(vobiz_public_url="https://voice.example.test/"),
    )
    token = "tenant-auth-token"
    message = "https://voice.example.test/webhooks/vobiz/inbound.nonce-123"
    signature = base64.b64encode(
        hmac.new(token.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()
    request = SimpleNamespace(
        headers={
            "X-Vobiz-Signature-V3": signature,
            "X-Vobiz-Signature-V3-Nonce": "nonce-123",
        }
    )

    assert VobizAdapter._valid_callback_signature(
        request, token, "/webhooks/vobiz/inbound"
    )


def test_rejects_missing_or_tampered_vobiz_signature(monkeypatch):
    monkeypatch.setattr(
        "chiefvoice.channels.vobiz.adapter.settings",
        SimpleNamespace(vobiz_public_url="https://voice.example.test"),
    )
    request = SimpleNamespace(
        headers={
            "X-Vobiz-Signature-V3": "invalid",
            "X-Vobiz-Signature-V3-Nonce": "nonce-123",
        }
    )

    assert not VobizAdapter._valid_callback_signature(
        request, "tenant-auth-token", "/webhooks/vobiz/inbound"
    )
