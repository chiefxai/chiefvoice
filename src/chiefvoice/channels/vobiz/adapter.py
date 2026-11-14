import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx
from cryptography.fernet import Fernet, InvalidToken
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
MAX_CALL_STATE_TOKEN_LENGTH = 16_384


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
        state_key = hashlib.sha256(
            b"chiefvoice-vobiz-state-encryption-v1:" + settings.vobiz_webhook_secret.encode()
        ).digest()
        self._state_cipher = Fernet(base64.urlsafe_b64encode(state_key))

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
            raise HTTPException(
                status_code=403, detail="Caller ID is not configured for this tenant"
            )

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
        auth_token = integration.credentials.get("auth_token", "")
        if not auth_token or not self._valid_callback_signature(
            request, auth_token, "/webhooks/vobiz/inbound"
        ):
            raise HTTPException(status_code=403, detail="Invalid Vobiz callback signature")
        return VobizInboundCall(
            provider=self.provider,
            channel="phone",
            tenant_id=integration.tenant_id,
            caller=caller,
            callee=callee,
            provider_call_id=call_id,
            bot_config_id=_optional_config_id(integration.settings.get("default_agent_id")),
        )

    @staticmethod
    def _valid_callback_signature(request: Request, auth_token: str, path: str) -> bool:
        """Validate Vobiz's V3 (preferred) or V2 callback HMAC.

        Vobiz signs the configured callback URL without its query string and a
        nonce. Use our configured public origin instead of proxy-controlled Host
        and forwarded headers. The route token remains an additional check.
        """
        public_url = (settings.vobiz_public_url or "").rstrip("/")
        base_url = f"{public_url}{path}"
        v3_signature = request.headers.get("X-Vobiz-Signature-V3", "")
        v3_nonce = request.headers.get("X-Vobiz-Signature-V3-Nonce", "")
        v2_signature = request.headers.get("X-Vobiz-Signature-V2", "")
        v2_nonce = request.headers.get("X-Vobiz-Signature-V2-Nonce", "")

        for signature, nonce, separator in (
            (v3_signature, v3_nonce, "."),
            (v2_signature, v2_nonce, ""),
        ):
            if not signature or not nonce or len(nonce) > 128:
                continue
            expected = base64.b64encode(
                hmac.new(
                    auth_token.encode(),
                    f"{base_url}{separator}{nonce}".encode(),
                    hashlib.sha256,
                ).digest()
            ).decode()
            if hmac.compare_digest(signature, expected):
                return True
        return False

    async def accept_inbound(
        self, call: InboundCall, session: AcceptedSession
    ) -> tuple[AcceptedSession, Response]:
        session.recording_allowed = session.recording_allowed and settings.vobiz_record_inbound
        # Coordinator persists and canonicalizes the answer timestamp before this.
        state = self._make_state(session)
        return session, self._answer_xml(state)

    async def reject_inbound(self, call: InboundCall, reason: str) -> Response:
        root = Element("Response")
        SubElement(root, "Hangup")
        return FastAPIResponse(
            content=tostring(root, encoding="unicode"),
            media_type="application/xml",
        )

    async def answer_outbound(self, request: Request) -> AcceptedSession:
        self._require_configured()
        state = request.query_params.get("state", "")
        claims = self._read_state(state)
        # The outbound API callback runs after the callee answers. Put that timestamp
        # into the media URL so billable duration excludes ringing and setup time.
        session = self._session_from_state(claims)
        session.started_at = datetime.now(UTC)
        form = await request.form()
        provider_call_id = _string_value(
            form.get("CallUUID") or form.get("RequestUUID")
        ) or _string_value(
            request.query_params.get("CallUUID") or request.query_params.get("RequestUUID")
        )
        if provider_call_id:
            session.provider_call_id = provider_call_id[:256]
        return session

    def outbound_answer_response(self, session: AcceptedSession) -> Response:
        """Create signed stream instructions after answer time is persisted."""
        return self._answer_xml(self._make_state(session))

    @staticmethod
    def reject_outbound_answer() -> Response:
        root = Element("Response")
        SubElement(root, "Hangup")
        return FastAPIResponse(
            content=tostring(root, encoding="unicode"), media_type="application/xml"
        )

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
        pipeline_factory: (
            Callable[[AcceptedSession, BaseTransport], Pipeline | Awaitable[Pipeline]] | None
        ),
        end_session: Callable[[AcceptedSession, str], Awaitable[None]],
        attach_session_persistence: Callable[[AcceptedSession], Any] | None = None,
    ) -> None:
        try:
            state = self._read_state(state_token)
        except HTTPException:
            await websocket.close(code=1008)
            return
        session = self._session_from_state(state)
        if attach_session_persistence is not None:
            persistence_result = attach_session_persistence(session)
            if inspect.isawaitable(persistence_result):
                await persistence_result
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
            runner_task = asyncio.create_task(
                PipelineRunner(handle_sigint=False).run(task),
                name=f"vobiz-pipeline-{session.session_id}",
            )
            timeout_task = None
            try:
                if session.max_call_seconds is None:
                    await runner_task
                else:
                    started_at = session.started_at
                    if started_at.tzinfo is None or started_at.utcoffset() is None:
                        raise ValueError("Call start time must include a timezone")
                    remaining_seconds = max(
                        0.0,
                        session.max_call_seconds
                        - (datetime.now(UTC) - started_at.astimezone(UTC)).total_seconds(),
                    )
                    timeout_task = asyncio.create_task(
                        asyncio.sleep(remaining_seconds),
                        name=f"vobiz-call-limit-{session.session_id}",
                    )
                    done, _pending = await asyncio.wait(
                        {runner_task, timeout_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if timeout_task in done and runner_task not in done:
                        end_reason = "timeout"
                        logger.warning(
                            "vobiz_media_session_max_duration_reached",
                            extra={
                                "session_id": session.session_id,
                                "provider": self.provider,
                                "max_call_seconds": session.max_call_seconds,
                                "remaining_seconds": remaining_seconds,
                            },
                        )
                        try:
                            await self.hangup(session, "max_call_duration_reached")
                        except Exception as exc:
                            logger.error(
                                "vobiz_max_duration_hangup_failed",
                                extra={
                                    "session_id": session.session_id,
                                    "provider": self.provider,
                                    "error_type": type(exc).__name__,
                                },
                            )
                        if websocket.client_state.name != "DISCONNECTED":
                            try:
                                await websocket.close(
                                    code=1000, reason="Maximum call duration reached"
                                )
                            except RuntimeError:
                                pass
                        try:
                            await asyncio.wait_for(runner_task, timeout=5.0)
                        except TimeoutError:
                            runner_task.cancel()
                            await asyncio.gather(runner_task, return_exceptions=True)
                    else:
                        await runner_task
            finally:
                if timeout_task is not None:
                    timeout_task.cancel()
                    await asyncio.gather(timeout_task, return_exceptions=True)
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
            response = await client.delete(
                endpoint, headers=self._auth_headers(auth_id, auth_token)
            )
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
                "bot_config_id": _optional_state_string(
                    session.bot_config_id, "bot_config_id", 128
                ),
                "max_call_seconds": session.max_call_seconds,
                "started_at": started_at.isoformat(),
            }
            call_context = {
                "live_agent_context": session.live_agent_context,
                "post_call_context": session.post_call_context,
                "post_call_output_schema": session.post_call_output_schema,
                "post_call_tools": session.post_call_tools,
                "post_call_time_zone": session.post_call_time_zone,
                "post_call_time_format": session.post_call_time_format,
                "include_live_agent_context": session.include_live_agent_context,
            }
            has_call_context = any(
                value is not None
                for name, value in call_context.items()
                if name != "include_live_agent_context"
            ) or session.include_live_agent_context
            if has_call_context:
                if not settings.credentials_encryption_key:
                    raise HTTPException(
                        status_code=503,
                        detail="Call context encryption is not configured",
                    )
                encrypted_context = Fernet(
                    settings.credentials_encryption_key.encode("ascii")
                ).encrypt(json.dumps(call_context, ensure_ascii=False).encode("utf-8"))
                claims["encrypted_call_context"] = encrypted_context.decode("ascii")
        except (AttributeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="Call state contains invalid session data"
            ) from exc
        signed_token = self._state.dumps(claims)
        token = self._state_cipher.encrypt(signed_token.encode("ascii")).decode("ascii")
        if len(token) > MAX_CALL_STATE_TOKEN_LENGTH:
            raise HTTPException(status_code=422, detail="Call state exceeds the supported size")
        return token

    def _read_state(self, token: str) -> dict[str, Any]:
        if not token or len(token) > MAX_CALL_STATE_TOKEN_LENGTH:
            raise HTTPException(status_code=404, detail="Not found")
        try:
            signed_token = self._state_cipher.decrypt(token.encode("ascii")).decode("ascii")
            claims = self._state.loads(signed_token, max_age=12 * 60 * 60)
        except (InvalidToken, UnicodeDecodeError, BadSignature, SignatureExpired) as exc:
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
            max_call_seconds = claims.get("max_call_seconds")
            if max_call_seconds is not None and (
                not isinstance(max_call_seconds, int)
                or isinstance(max_call_seconds, bool)
                or not 1 <= max_call_seconds <= 3_600
            ):
                raise ValueError("Invalid max call duration")
            encrypted_context = claims.get("encrypted_call_context")
            if encrypted_context is not None:
                if (
                    not isinstance(encrypted_context, str)
                    or not settings.credentials_encryption_key
                ):
                    raise ValueError("Invalid encrypted call context")
                try:
                    call_context = json.loads(
                        Fernet(settings.credentials_encryption_key.encode("ascii"))
                        .decrypt(encrypted_context.encode("ascii"))
                        .decode("utf-8")
                    )
                except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("Invalid encrypted call context") from exc
                if not isinstance(call_context, dict):
                    raise ValueError("Invalid call context")
                live_context = call_context.get("live_agent_context")
                post_context = call_context.get("post_call_context")
                output_schema = call_context.get("post_call_output_schema")
                post_call_tools = call_context.get("post_call_tools")
                time_zone = call_context.get("post_call_time_zone")
                time_format = call_context.get("post_call_time_format")
                include_live_context = call_context.get("include_live_agent_context", False)
                if live_context is not None and not isinstance(live_context, dict):
                    raise ValueError("Invalid live agent context")
                if post_context is not None and not isinstance(post_context, str):
                    raise ValueError("Invalid post-call context")
                if output_schema is not None and not isinstance(output_schema, dict):
                    raise ValueError("Invalid post-call output schema")
                if post_call_tools is not None and (
                    not isinstance(post_call_tools, list)
                    or any(
                        not isinstance(tool, str) or tool not in {"calculator", "time"}
                        for tool in post_call_tools
                    )
                    or len(set(post_call_tools)) != len(post_call_tools)
                ):
                    raise ValueError("Invalid post-call tools")
                if time_zone is not None and not isinstance(time_zone, str):
                    raise ValueError("Invalid post-call time zone")
                if time_format is not None and not isinstance(time_format, str):
                    raise ValueError("Invalid post-call time format")
                if not isinstance(include_live_context, bool):
                    raise ValueError("Invalid live agent context flag")
                claims["call_context"] = call_context
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
            max_call_seconds=claims.get("max_call_seconds"),
            live_agent_context=claims.get("call_context", {}).get("live_agent_context"),
            post_call_context=claims.get("call_context", {}).get("post_call_context"),
            post_call_output_schema=claims.get("call_context", {}).get(
                "post_call_output_schema"
            ),
            post_call_tools=claims.get("call_context", {}).get("post_call_tools"),
            post_call_time_zone=claims.get("call_context", {}).get("post_call_time_zone"),
            post_call_time_format=claims.get("call_context", {}).get("post_call_time_format"),
            include_live_agent_context=claims.get("call_context", {}).get(
                "include_live_agent_context", False
            ),
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
