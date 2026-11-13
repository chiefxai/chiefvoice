import hmac
import inspect
import logging
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx
from fastapi import HTTPException, Request, Response, WebSocket
from fastapi.responses import Response as FastAPIResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.serializers.vobiz import VobizFrameSerializer, parse_vobiz_start
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from chiefvoice.channels.base import InboundCall, OutboundCall
from chiefvoice.channels.integrations import ProviderIntegrationStore
from chiefvoice.core.config import settings
from chiefvoice.sessions import AcceptedSession, Direction

logger = logging.getLogger(__name__)
VOBIZ_API = "https://api.vobiz.ai"
PIPELINE_SAMPLE_RATE = 8000
MAX_CALL_STATE_TOKEN_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class VobizInboundCall:
    provider: str
    channel: str
    tenant_id: str
    caller: str | None
    callee: str | None
    provider_call_id: str | None
    bot_config_id: str | None = None
    direction: Direction = Direction.INBOUND


class VobizAdapter:
    """Tenant-aware Vobiz adapter; account credentials are resolved per call."""

    name = "vobiz"
    provider = "vobiz"

    def __init__(self, integration_store: ProviderIntegrationStore):
        self.integration_store = integration_store
        if not settings.vobiz_webhook_secret:
            raise ValueError("VOBIZ_WEBHOOK_SECRET is required")
        if settings.vobiz_public_url and not settings.vobiz_public_url.startswith("https://"):
            raise ValueError("VOBIZ_PUBLIC_URL must use HTTPS")
        if settings.vobiz_sample_rate not in {8000, 16000}:
            raise ValueError("VOBIZ_SAMPLE_RATE must be 8000 or 16000")
        if settings.vobiz_encoding not in {"audio/x-mulaw", "audio/x-l16"}:
            raise ValueError("VOBIZ_ENCODING must be audio/x-mulaw or audio/x-l16")
        self._state = URLSafeTimedSerializer(
            settings.vobiz_webhook_secret, salt="chiefvoice-vobiz-call-state-v1"
        )

    @property
    def configured(self) -> bool:
        return all(
            (
                settings.vobiz_public_url,
                settings.vobiz_inbound_route_token,
                settings.vobiz_webhook_secret,
            )
        )

    async def initiate_outbound(
        self, call: OutboundCall, session: AcceptedSession
    ) -> AcceptedSession:
        self._require_configured()
        integration = await self.integration_store.get(self.provider, call.tenant_id)
        if integration is None:
            raise HTTPException(status_code=403, detail="Vobiz is not connected to this account")
        auth_id = integration.credentials.get("auth_id", "")
        auth_token = integration.credentials.get("auth_token", "")
        caller_id = call.caller_id or str(integration.settings.get("default_caller_id", ""))
        if not auth_id or not auth_token:
            raise HTTPException(status_code=403, detail="Vobiz credentials are incomplete")
        if caller_id not in {n.number for n in integration.phone_numbers if n.outbound_enabled}:
            raise HTTPException(status_code=403, detail="Caller ID is not configured for this tenant")

        # These values are known before asking Vobiz to place the call. Preserve them
        # in the signed callback state so the media session gets the same call context.
        session.caller = caller_id
        session.callee = call.destination
        state = self._make_state(session)
        answer_url = self._public_url("/webhooks/vobiz/answer", {"state": state})
        hangup_url = self._public_url("/webhooks/vobiz/hangup", {"state": state})
        endpoint = f"{VOBIZ_API}/api/v1/Account/{auth_id}/Call/"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
                response = await client.post(
                    endpoint,
                    headers=self._auth_headers(auth_id, auth_token),
                    json={
                        "from": caller_id,
                        "to": call.destination,
                        "answer_url": answer_url,
                        "answer_method": "POST",
                        "hangup_url": hangup_url,
                        "hangup_method": "POST",
                    },
                )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.error(
                "vobiz_outbound_request_failed",
                extra={
                    "session_id": session.session_id,
                    "provider": self.provider,
                    "error_type": type(exc).__name__,
                },
            )
            raise HTTPException(status_code=502, detail="Vobiz could not start the call") from exc

        call_id = result.get("request_uuid") or result.get("call_uuid")
        if not isinstance(call_id, str) or not call_id:
            raise HTTPException(status_code=502, detail="Vobiz response did not include a call ID")
        session.provider_call_id = call_id
        logger.info(
            "vobiz_call_queued",
            extra={
                "session_id": session.session_id,
                "provider": self.provider,
                "channel": session.channel,
            },
        )
        return session

    async def parse_inbound(self, request: Request) -> InboundCall:
        self._require_configured()
        supplied_route_token = request.query_params.get("key", "")
        if not hmac.compare_digest(supplied_route_token, settings.vobiz_inbound_route_token or ""):
            raise HTTPException(status_code=404, detail="Not found")
        form = await request.form()
        caller = _string_value(form.get("From"))
        callee = _string_value(form.get("To"))
        call_id = _string_value(form.get("CallUUID") or form.get("RequestUUID"))
        integration = (
            await self.integration_store.get_for_inbound_number(self.provider, callee)
            if callee
            else None
        )
        if integration is None:
            raise HTTPException(status_code=404, detail="Inbound number is not configured")
        return VobizInboundCall(
            provider=self.provider,
            channel="phone",
            tenant_id=integration.tenant_id,
            caller=caller,
            callee=callee,
            provider_call_id=call_id,
            bot_config_id=_optional_config_id(integration.settings.get("default_agent_id")),
        )

    async def accept_inbound(
        self, call: InboundCall, session: AcceptedSession
    ) -> tuple[AcceptedSession, Response]:
        session.recording_allowed = session.recording_allowed and settings.vobiz_record_inbound
        # Inbound calls begin billing when the provider is answered, not when the
        # webhook first arrived and subscription admission ran.
        session.started_at = datetime.now(UTC)
        state = self._make_state(session)
        return session, self._answer_xml(state)

    async def reject_inbound(self, call: InboundCall, reason: str) -> Response:
        root = Element("Response")
        SubElement(root, "Hangup")
        return FastAPIResponse(
            content=tostring(root, encoding="unicode"),
            media_type="application/xml",
        )

    async def answer_outbound(self, request: Request) -> Response:
        self._require_configured()
        state = request.query_params.get("state", "")
        claims = self._read_state(state)
        # The outbound API callback runs after the callee answers. Put that timestamp
        # into the media URL so billable duration excludes ringing and setup time.
        claims["started_at"] = datetime.now(UTC).isoformat()
        answered_state = self._state.dumps(claims)
        return self._answer_xml(answered_state)

    async def hangup_callback(self, request: Request) -> AcceptedSession:
        state = self._read_state(request.query_params.get("state", ""))
        session = self._session_from_state(state)
        # Vobiz may send the provider call ID in the callback body. The outbound
        # callback URL is created before the provider returns that ID, so use the
        # signed session state as the identity and the callback body only to enrich it.
        form = await request.form()
        provider_call_id = _string_value(form.get("CallUUID") or form.get("RequestUUID"))
        if provider_call_id:
            session.provider_call_id = provider_call_id[:256]
        return session

    async def build_transport(self, session: AcceptedSession, connection: object) -> BaseTransport:
        if not isinstance(connection, WebSocket):
            raise TypeError("Vobiz media transport requires a FastAPI WebSocket")
        self._require_configured()
        integration = await self.integration_store.get(self.provider, session.tenant_id)
        if integration is None:
            raise HTTPException(status_code=404, detail="Vobiz account is no longer configured")
        auth_id = integration.credentials.get("auth_id", "")
        auth_token = integration.credentials.get("auth_token", "")
        if not auth_id or not auth_token:
            raise HTTPException(status_code=404, detail="Vobiz credentials are incomplete")
        await connection.accept()
        parsed = await parse_vobiz_start(connection)
        session.provider_call_id = parsed["call_id"]
        serializer = VobizFrameSerializer(
            stream_id=parsed["stream_id"],
            call_id=parsed["call_id"],
            auth_id=auth_id,
            auth_token=auth_token,
            params=VobizFrameSerializer.InputParams(
                vobiz_sample_rate=parsed["sample_rate"] or settings.vobiz_sample_rate,
                encoding=parsed["encoding"] or settings.vobiz_encoding,
                sample_rate=None,
                auto_hang_up=True,
            ),
        )
        logger.info(
            "vobiz_media_connected",
            extra={"session_id": session.session_id, "provider": self.provider},
        )
        return FastAPIWebsocketTransport(
            websocket=connection,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                add_wav_header=False,
                serializer=serializer,
            ),
        )

    async def run_media_session(
        self,
        websocket: WebSocket,
        state_token: str,
        pipeline_factory: Callable[[AcceptedSession, BaseTransport], Pipeline | Awaitable[Pipeline]] | None,
        end_session: Callable[[AcceptedSession, str], Awaitable[None]],
    ) -> None:
        try:
            state = self._read_state(state_token)
        except HTTPException:
            await websocket.close(code=1008)
            return
        session = self._session_from_state(state)
        end_reason = "completed"
        try:
            transport = await self.build_transport(session, websocket)
            if pipeline_factory is None:
                end_reason = "provider_error"
                await self.hangup(session, "voice pipeline is not configured")
                await websocket.close(code=1011, reason="Voice pipeline is not configured")
                return
            pipeline = pipeline_factory(session, transport)
            if inspect.isawaitable(pipeline):
                pipeline = await pipeline
            from pipecat.pipeline.runner import PipelineRunner
            from pipecat.pipeline.task import PipelineParams, PipelineTask

            task = PipelineTask(
                pipeline,
                params=PipelineParams(
                    audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
                    audio_out_sample_rate=PIPELINE_SAMPLE_RATE,
                    enable_metrics=True,
                    enable_usage_metrics=True,
                ),
            )
            await PipelineRunner(handle_sigint=False).run(task)
        except Exception as exc:
            end_reason = "provider_error"
            logger.error(
                "vobiz_media_session_failed",
                extra={
                    "session_id": session.session_id,
                    "provider": self.provider,
                    "error_type": type(exc).__name__,
                },
            )
            if websocket.client_state.name != "DISCONNECTED":
                try:
                    await websocket.close(code=1011)
                except RuntimeError:
                    pass
            raise
        finally:
            await end_session(session, end_reason)

    async def hangup(self, session: AcceptedSession, reason: str | None = None) -> None:
        if not session.provider_call_id:
            return
        self._require_configured()
        integration = await self.integration_store.get(self.provider, session.tenant_id)
        if integration is None:
            return
        auth_id = integration.credentials.get("auth_id", "")
        auth_token = integration.credentials.get("auth_token", "")
        if not auth_id or not auth_token:
            return
        endpoint = (
            f"{VOBIZ_API}/api/v1/Account/{auth_id}/Call/"
            f"{urllib.parse.quote(session.provider_call_id, safe='')}/"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.delete(endpoint, headers=self._auth_headers(auth_id, auth_token))
            response.raise_for_status()

    async def transfer(self, session: AcceptedSession, destination: str) -> None:
        raise HTTPException(status_code=501, detail="Vobiz transfer is not implemented yet")

    def _answer_xml(self, state_token: str) -> Response:
        self._require_configured()
        root = Element("Response")
        stream = SubElement(
            root,
            "Stream",
            {
                "bidirectional": "true",
                "audioTrack": "inbound",
                "contentType": f"{settings.vobiz_encoding};rate={settings.vobiz_sample_rate}",
                "keepCallAlive": "true",
            },
        )
        stream_url = self._public_url("/webhooks/vobiz/media", {"state": state_token})
        stream.text = stream_url.replace("https://", "wss://", 1)
        return FastAPIResponse(
            content='<?xml version="1.0" encoding="UTF-8"?>' + tostring(root, encoding="unicode"),
            media_type="application/xml",
        )

    def _make_state(self, session: AcceptedSession) -> str:
        try:
            started_at = session.started_at.astimezone(UTC)
            if session.started_at.tzinfo is None or session.started_at.utcoffset() is None:
                raise ValueError("started_at must include a timezone")
            claims = {
                "session_id": _required_state_string(session.session_id, "session_id", 128),
                "tenant_id": _required_state_string(session.tenant_id, "tenant_id", 200),
                "direction": session.direction.value,
                "recording_allowed": session.recording_allowed is True,
                "caller": _optional_state_string(session.caller, "caller", 256),
                "callee": _optional_state_string(session.callee, "callee", 256),
                "provider_call_id": _optional_state_string(
                    session.provider_call_id, "provider_call_id", 256
                ),
                "bot_config_id": _optional_state_string(session.bot_config_id, "bot_config_id", 128),
                "started_at": started_at.isoformat(),
            }
        except (AttributeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="Call state contains invalid session data"
            ) from exc
        token = self._state.dumps(claims)
        if len(token) > MAX_CALL_STATE_TOKEN_LENGTH:
            raise HTTPException(status_code=422, detail="Call state exceeds the supported size")
        return token

    def _read_state(self, token: str) -> dict[str, Any]:
        if not token or len(token) > MAX_CALL_STATE_TOKEN_LENGTH:
            raise HTTPException(status_code=404, detail="Not found")
        try:
            claims = self._state.loads(token, max_age=12 * 60 * 60)
        except (BadSignature, SignatureExpired) as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc
        if not isinstance(claims, dict):
            raise HTTPException(status_code=404, detail="Not found")
        try:
            _required_state_string(claims.get("session_id"), "session_id", 128)
            _required_state_string(claims.get("tenant_id"), "tenant_id", 200)
            if claims.get("direction") not in {Direction.INBOUND.value, Direction.OUTBOUND.value}:
                raise ValueError("Invalid direction")
            if not isinstance(claims.get("recording_allowed"), bool):
                raise ValueError("Invalid recording flag")
            for field, limit in (
                ("caller", 256),
                ("callee", 256),
                ("provider_call_id", 256),
                ("bot_config_id", 128),
            ):
                _optional_state_string(claims.get(field), field, limit)
            _parse_state_datetime(claims.get("started_at"))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Not found") from exc
        return claims

    @staticmethod
    def _session_from_state(claims: dict[str, Any]) -> AcceptedSession:
        return AcceptedSession(
            channel="phone",
            provider="vobiz",
            direction=Direction(claims["direction"]),
            caller=claims.get("caller"),
            callee=claims.get("callee"),
            provider_call_id=claims.get("provider_call_id"),
            tenant_id=claims["tenant_id"],
            bot_config_id=claims.get("bot_config_id"),
            session_id=claims["session_id"],
            recording_allowed=claims.get("recording_allowed") is True,
            started_at=_parse_state_datetime(claims["started_at"]),
        )

    def _public_url(self, path: str, query: dict[str, str]) -> str:
        base = (settings.vobiz_public_url or "").rstrip("/")
        return f"{base}{path}?{urllib.parse.urlencode(query)}"

    @staticmethod
    def _auth_headers(auth_id: str, auth_token: str) -> dict[str, str]:
        return {
            "X-Auth-ID": auth_id,
            "X-Auth-Token": auth_token,
            "Content-Type": "application/json",
        }

    def _require_configured(self) -> None:
        if not self.configured:
            raise HTTPException(status_code=503, detail="Vobiz is not fully configured")


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_config_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:128] if value else None


def _required_state_string(value: Any, name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(f"Invalid {name}")
    return value


def _optional_state_string(value: Any, name: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError(f"Invalid {name}")
    return value


def _parse_state_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid started_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Invalid started_at") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("started_at must include a timezone")
    return parsed.astimezone(UTC)
