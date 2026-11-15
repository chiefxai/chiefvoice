from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from chiefvoice.channels.vobiz.adapter import VobizAdapter
from chiefvoice.observability.setup import UvicornQueryRedactionFilter
from chiefvoice.sessions import AcceptedSession, Direction


def _settings():
    return SimpleNamespace(
        vobiz_webhook_secret="a-long-test-webhook-secret-value",
        vobiz_public_url="https://voice.example.test",
        vobiz_inbound_route_token="route-secret",
        vobiz_sample_rate=8000,
        vobiz_encoding="audio/x-mulaw",
        credentials_encryption_key=Fernet.generate_key().decode(),
    )


def test_callback_state_hides_personal_data_and_round_trips(monkeypatch):
    monkeypatch.setattr("chiefvoice.channels.vobiz.adapter.settings", _settings())
    adapter = VobizAdapter(integration_store=object())  # State methods do not access the store.
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        tenant_id="tenant-private-123",
        caller="+15555550111",
        callee="+15555550222",
    )

    token = adapter._make_state(session)

    assert "tenant-private-123" not in token
    assert "+15555550111" not in token
    assert "+15555550222" not in token
    claims = adapter._read_state(token)
    assert claims["tenant_id"] == "tenant-private-123"
    assert claims["caller"] == "+15555550111"
    assert claims["callee"] == "+15555550222"


def test_callback_envelope_encrypts_full_context_and_identity(monkeypatch):
    monkeypatch.setattr("chiefvoice.channels.vobiz.adapter.settings", _settings())
    adapter = VobizAdapter(integration_store=object())
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        tenant_id="tenant-private-456",
        caller="+15555550333",
        callee="+15555550444",
        live_agent_context={"patient_name": "Private Patient"},
        post_call_context="Extract the appointment outcome",
        post_call_output_schema={"properties": {"outcome": {"type": "string"}}},
        post_call_tools=["time", "calculator"],
        post_call_time_zone="Asia/Kolkata",
        post_call_time_format="%Y-%m-%d %H:%M:%S%z",
        include_live_agent_context=True,
    )

    token = adapter._make_state(session)

    for private_value in (
        "tenant-private-456",
        "+15555550333",
        "+15555550444",
        "Private Patient",
        "Extract the appointment outcome",
        "Asia/Kolkata",
    ):
        assert private_value not in token
    claims = adapter._read_state(token)
    assert claims["tenant_id"] == "tenant-private-456"
    assert claims["call_context"]["live_agent_context"] == {
        "patient_name": "Private Patient"
    }
    assert claims["call_context"]["include_live_agent_context"] is True


def test_callback_state_rejects_modified_ciphertext(monkeypatch):
    monkeypatch.setattr("chiefvoice.channels.vobiz.adapter.settings", _settings())
    adapter = VobizAdapter(integration_store=object())
    session = AcceptedSession(
        channel="phone",
        provider="vobiz",
        direction=Direction.OUTBOUND,
        tenant_id="tenant-1",
    )

    token = adapter._make_state(session)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    with pytest.raises(HTTPException) as error:
        adapter._read_state(tampered)
    assert error.value.status_code == 404


def test_callback_state_rejection_logs_safe_reason_without_token(monkeypatch, caplog):
    monkeypatch.setattr("chiefvoice.channels.vobiz.adapter.settings", _settings())
    adapter = VobizAdapter(integration_store=object())
    token = "not-a-valid-token"

    with pytest.raises(HTTPException):
        adapter._read_state(token)

    record = next(
        record
        for record in caplog.records
        if record.message == "vobiz_callback_state_rejected"
    )
    assert record.reason == "encryption_invalid"
    assert record.state_length == len(token)
    assert token not in record.getMessage()


def test_callback_state_rejects_non_ascii_token_safely(monkeypatch, caplog):
    monkeypatch.setattr("chiefvoice.channels.vobiz.adapter.settings", _settings())
    adapter = VobizAdapter(integration_store=object())

    with pytest.raises(HTTPException):
        adapter._read_state("not-ascii-🔒")

    record = next(
        record
        for record in caplog.records
        if record.message == "vobiz_callback_state_rejected"
    )
    assert record.reason == "encoding_invalid"


def test_uvicorn_websocket_logs_redact_callback_query_string():
    record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        __file__,
        1,
        '%s - "WebSocket %s" 403',
        ("127.0.0.1", "/webhooks/vobiz/media?state=secret-token"),
        None,
    )

    assert UvicornQueryRedactionFilter().filter(record)
    assert "secret-token" not in record.getMessage()
    assert "/webhooks/vobiz/media" in record.getMessage()
