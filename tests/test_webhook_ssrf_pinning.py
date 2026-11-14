import socket

import pytest

from chiefvoice.agents.post_call.queue import _resolve_public_webhook_target_sync


def test_webhook_target_resolves_to_pinned_public_ip(monkeypatch):
    # TEST-NET is intentionally non-global, so use a representative globally routed address.
    monkeypatch.setattr(
        "chiefvoice.agents.post_call.queue.socket.getaddrinfo",
        lambda host, port, *, type: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )
    parsed, pinned = _resolve_public_webhook_target_sync(
        "https://hooks.example.com/results?tenant=one"
    )
    assert parsed.hostname == "hooks.example.com"
    assert parsed.path == "/results"
    assert parsed.query == "tenant=one"
    assert pinned == "8.8.8.8"


def test_webhook_target_rejects_mixed_public_and_private_dns_answers(monkeypatch):
    monkeypatch.setattr(
        "chiefvoice.agents.post_call.queue.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(ValueError, match="resolve only to public IP"):
        _resolve_public_webhook_target_sync("https://hooks.example.com/results")
