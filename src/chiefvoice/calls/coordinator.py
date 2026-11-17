import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException, Request, Response
from pipecat.transcriptions.language import Language

from chiefvoice.agents.call.configs.profiles import AgentProfileDirectory
from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.agents.post_call.fallback_spool import PostCallFallbackSpool
from chiefvoice.agents.post_call.queue import PostCallDispatcher
from chiefvoice.billing import SubscriptionAuthorizer
from chiefvoice.calls.store import IdempotencyConflict, SqlCallSessionStore
from chiefvoice.channels.base import AmbiguousCallStart, ChannelAdapter, OutboundCall
from chiefvoice.cloud_projects.models import TenantVertexProjectDirectory
from chiefvoice.identity.api_keys import ApiKeyDirectory
from chiefvoice.observability.setup import (
    call_duration_limit_event,
    post_call_enqueue_fallback_event,
    post_call_missing_transcript,
    session_ended,
    session_started,
)
from chiefvoice.sessions import AcceptedSession, Direction

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OutboundCallCommand:
    provider: str
    channel: str
    tenant_id: str
    destination: str
    api_key_id: str | None = None
    caller_id: str | None = None
    bot_config_id: str | None = None
    live_agent_instructions: str | None = None
    live_agent_template_id: str | None = None
    live_agent_template_revision: int | None = None
    live_agent_context: dict[str, Any] | None = None
    live_agent_language: str | None = None
    live_agent_completion_requirements: dict[str, Any] | None = None
    post_call_context: str | None = None
    post_call_template_id: str | None = None
    post_call_template_revision: int | None = None
    post_call_output_schema: dict[str, Any] | None = None
    post_call_tools: list[str] | None = None
    post_call_time_zone: str | None = None
    post_call_time_format: str | None = None
    include_live_agent_context: bool = False
    recording_allowed: bool = False
    auto_hangup_when_complete: bool = False
    idempotency_key: str | None = None


class CallCoordinator:
    def __init__(
        self,
        adapters: Mapping[str, ChannelAdapter],
        subscriptions: SubscriptionAuthorizer,
        *,
        post_call_dispatcher: PostCallDispatcher | None = None,
        post_call_required: bool = True,
        post_call_available: bool = False,
        agent_profile_directory: AgentProfileDirectory | None = None,
        post_call_fallback_spool: PostCallFallbackSpool | None = None,
        vertex_project_directory: TenantVertexProjectDirectory | None = None,
        require_tenant_vertex_project: bool = False,
        call_session_store: SqlCallSessionStore | None = None,
        api_key_directory: ApiKeyDirectory | None = None,
        realtime_preflight: Callable[[AcceptedSession], Awaitable[None]] | None = None,
        realtime_preflight_cleanup: Callable[[str, str], Awaitable[None]] | None = None,
    ):
        self.adapters = adapters
        self.subscriptions = subscriptions
        self.post_call_dispatcher = post_call_dispatcher
        self.post_call_required = post_call_required
        self.post_call_available = post_call_available
        self.agent_profile_directory = agent_profile_directory
        self.post_call_fallback_spool = post_call_fallback_spool
        self.vertex_project_directory = vertex_project_directory
        self.require_tenant_vertex_project = require_tenant_vertex_project
        self.call_session_store = call_session_store
        self.api_key_directory = api_key_directory
        self.realtime_preflight = realtime_preflight
        self.realtime_preflight_cleanup = realtime_preflight_cleanup

    async def start_outbound(self, command: OutboundCallCommand) -> AcceptedSession:
        if command.live_agent_language:
            try:
                Language(command.live_agent_language)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail="The selected realtime provider does not support the calling template language",
                ) from exc
        post_call_agent_enabled = True
        auto_hangup_when_complete = command.auto_hangup_when_complete
        if command.api_key_id is not None:
            if self.api_key_directory is None:
                raise HTTPException(status_code=503, detail="API agent configuration is unavailable")
            api_config = await self.api_key_directory.get_vertex_config(command.api_key_id)
            if api_config is None:
                raise HTTPException(status_code=503, detail="Configure GCP credentials for this API before placing calls")
            if not api_config.voice_agent_enabled:
                raise HTTPException(status_code=409, detail="The calling agent is disabled for this API")
            if not api_config.project_id or not api_config.credentials_json:
                raise HTTPException(status_code=503, detail="Configure Google Cloud credentials for this API before placing calls")
            post_call_agent_enabled = api_config.post_call_agent_enabled
            auto_hangup_when_complete = auto_hangup_when_complete or api_config.auto_hangup_when_complete
        has_legacy_requirements = bool(
            isinstance(command.live_agent_context, dict)
            and command.live_agent_context.get("mandatory_fields")
        )
        if (
            auto_hangup_when_complete
            and not command.live_agent_completion_requirements
            and not has_legacy_requirements
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Automatic hang-up requires live_agent template completion_requirements "
                    "or live_agent.context.mandatory_fields"
                ),
            )
        if post_call_agent_enabled:
            self._require_post_call_ready()
        await self._require_vertex_project(command.tenant_id)
        adapter = self._adapter(command.provider)
        if command.bot_config_id is not None:
            if self.agent_profile_directory is None:
                raise HTTPException(
                    status_code=503, detail="Voice agent profile storage is not configured"
                )
            profile = await self.agent_profile_directory.get(
                command.tenant_id, command.bot_config_id
            )
            if profile is None:
                raise HTTPException(status_code=404, detail="Voice agent profile not found")
            if (
                command.live_agent_language
                and command.live_agent_language.split("-", 1)[0].lower()
                != profile.language.split("-", 1)[0].lower()
            ):
                raise HTTPException(
                    status_code=422,
                    detail="Calling template language must match the selected voice profile language",
                )
        session = AcceptedSession(
            channel=command.channel,
            provider=command.provider,
            direction=Direction.OUTBOUND,
            caller=command.caller_id,
            callee=command.destination,
            tenant_id=command.tenant_id,
            api_key_id=command.api_key_id,
            bot_config_id=command.bot_config_id,
            live_agent_instructions=command.live_agent_instructions,
            live_agent_template_id=command.live_agent_template_id,
            live_agent_template_revision=command.live_agent_template_revision,
            live_agent_context=command.live_agent_context,
            live_agent_language=command.live_agent_language,
            live_agent_completion_requirements=command.live_agent_completion_requirements,
            post_call_context=command.post_call_context,
            post_call_template_id=command.post_call_template_id,
            post_call_template_revision=command.post_call_template_revision,
            post_call_output_schema=command.post_call_output_schema,
            post_call_tools=command.post_call_tools,
            post_call_time_zone=command.post_call_time_zone,
            post_call_time_format=command.post_call_time_format,
            include_live_agent_context=command.include_live_agent_context,
            post_call_agent_enabled=post_call_agent_enabled,
            auto_hangup_when_complete=auto_hangup_when_complete,
        )
        fingerprint = SqlCallSessionStore.fingerprint(
            {
                "provider": command.provider,
                "channel": command.channel,
                "api_key_id": command.api_key_id,
                "destination": command.destination,
                "caller_id": command.caller_id,
                "bot_config_id": command.bot_config_id,
                "live_agent_instructions": command.live_agent_instructions,
                "live_agent_template_id": command.live_agent_template_id,
                "live_agent_template_revision": command.live_agent_template_revision,
                "live_agent_context": command.live_agent_context,
                "live_agent_language": command.live_agent_language,
                "live_agent_completion_requirements": command.live_agent_completion_requirements,
                "post_call_context": command.post_call_context,
                "post_call_template_id": command.post_call_template_id,
                "post_call_template_revision": command.post_call_template_revision,
                "post_call_output_schema": command.post_call_output_schema,
                "post_call_tools": command.post_call_tools,
                "post_call_time_zone": command.post_call_time_zone,
                "post_call_time_format": command.post_call_time_format,
                "include_live_agent_context": command.include_live_agent_context,
                "post_call_agent_enabled": post_call_agent_enabled,
                "recording_allowed": command.recording_allowed,
            }
        )
        if command.idempotency_key and self.call_session_store is None:
            raise HTTPException(
                status_code=503,
                detail="Durable idempotency storage is not configured",
            )
        if self.call_session_store is not None:
            try:
                session, created, previous_status = await self.call_session_store.register_outbound(
                    session, command.idempotency_key, fingerprint
                )
            except IdempotencyConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if not created:
                if previous_status == "failed":
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "This idempotency key belongs to a failed call; "
                            "use a new key to start another call"
                        ),
                    )
                self.attach_session_persistence(session)
                session.metadata["call_status"] = previous_status
                return session
        self.attach_session_persistence(session)
        session.metadata["call_status"] = "starting"
        try:
            if command.api_key_id is None:
                entitlement = await self.subscriptions.authorize_voice(
                    command.tenant_id, session.session_id, command.channel, Direction.OUTBOUND
                )
            else:
                entitlement = await self.subscriptions.authorize_voice(
                    command.tenant_id, session.session_id, command.channel,
                    Direction.OUTBOUND, command.api_key_id,
                )
        except BaseException:
            if self.call_session_store is not None:
                await self.call_session_store.update_state(
                    session, status="failed", end_reason="billing_authorization_failed"
                )
            raise
        if self.realtime_preflight is not None:
            try:
                await self.realtime_preflight(session)
            except BaseException as exc:
                await self.subscriptions.release_voice(command.tenant_id, session.session_id)
                if self.call_session_store is not None:
                    await self.call_session_store.update_state(
                        session, status="failed", end_reason="realtime_preflight_failed"
                    )
                if isinstance(exc, HTTPException):
                    raise
                logger.warning(
                    "realtime_preflight_failed",
                    extra={
                        "session_id": session.session_id,
                        "provider": command.provider,
                        "error_type": type(exc).__name__,
                    },
                )
                raise HTTPException(
                    status_code=503,
                    detail="The live AI provider is unavailable; no call was placed.",
                ) from exc
        call: OutboundCall = _OutboundCallData(
            provider=command.provider,
            channel=command.channel,
            tenant_id=command.tenant_id,
            destination=command.destination,
            api_key_id=command.api_key_id,
            caller_id=command.caller_id,
            bot_config_id=command.bot_config_id,
            live_agent_instructions=command.live_agent_instructions,
            live_agent_context=command.live_agent_context,
            post_call_context=command.post_call_context,
            post_call_output_schema=command.post_call_output_schema,
            post_call_tools=command.post_call_tools,
            post_call_time_zone=command.post_call_time_zone,
            post_call_time_format=command.post_call_time_format,
            include_live_agent_context=command.include_live_agent_context,
            recording_allowed=command.recording_allowed and entitlement.recording_allowed,
            session_id=session.session_id,
        )
        session.recording_allowed = call.recording_allowed
        session.max_call_seconds = entitlement.max_call_seconds
        if self.call_session_store is not None:
            try:
                await self.call_session_store.update_state(session, status="starting")
            except BaseException:
                if self.realtime_preflight_cleanup is not None:
                    await self.realtime_preflight_cleanup(
                        session.session_id, "call_state_persist_failed"
                    )
                await self.subscriptions.release_voice(command.tenant_id, session.session_id)
                await self.call_session_store.update_state(
                    session, status="failed", end_reason="call_state_persist_failed"
                )
                raise
        try:
            session = await adapter.initiate_outbound(call, session)
        except AmbiguousCallStart as exc:
            # Keep the prepaid/postpaid reservation in place: the provider may have
            # accepted the request despite losing its response. The same idempotency
            # key must return this session instead of risking a duplicate paid call.
            if self.call_session_store is not None:
                await self.call_session_store.update_state(
                    session,
                    status="reconciliation_required",
                    end_reason="provider_start_outcome_ambiguous",
                )
            logger.error(
                "outbound_call_requires_reconciliation",
                extra={
                    "session_id": session.session_id,
                    "tenant_id": session.tenant_id,
                    "provider": session.provider,
                    "error_type": type(exc).__name__,
                },
            )
            session.metadata["call_status"] = "reconciliation_required"
            return session
        except BaseException:
            if self.realtime_preflight_cleanup is not None:
                await self.realtime_preflight_cleanup(session.session_id, "provider_start_failed")
            await self.subscriptions.release_voice(command.tenant_id, session.session_id)
            if self.call_session_store is not None:
                await self.call_session_store.update_state(
                    session, status="failed", end_reason="provider_start_failed"
                )
            raise
        if self.call_session_store is not None:
            await self.call_session_store.update_state(
                session, status="initiated", provider_call_id=session.provider_call_id
            )
        session.metadata["call_status"] = "initiated"
        session_started(session.channel, session.provider)
        return session

    async def handle_inbound(self, provider: str, request: Request) -> Response:
        adapter = self._adapter(provider)
        inbound = await adapter.parse_inbound(request)
        try:
            await self._require_vertex_project(inbound.tenant_id)
        except HTTPException as exc:
            return await adapter.reject_inbound(inbound, str(exc.detail))
        bot_config_id = getattr(inbound, "bot_config_id", None)
        if bot_config_id is not None:
            if self.agent_profile_directory is None:
                return await adapter.reject_inbound(
                    inbound, "Voice agent profile storage is not configured"
                )
            profile = await self.agent_profile_directory.get(inbound.tenant_id, bot_config_id)
            if profile is None:
                return await adapter.reject_inbound(inbound, "Configured voice agent was not found")
        if self.post_call_required and not self.post_call_available:
            return await adapter.reject_inbound(
                inbound,
                "Post-call analysis is enabled but its agent or durable queue is not configured",
            )
        session = AcceptedSession(
            channel=inbound.channel,
            provider=inbound.provider,
            direction=Direction.INBOUND,
            caller=inbound.caller,
            callee=inbound.callee,
            provider_call_id=inbound.provider_call_id,
            tenant_id=inbound.tenant_id,
            bot_config_id=bot_config_id,
            session_id=(
                str(uuid5(NAMESPACE_URL, f"chiefvoice:{provider}:{inbound.provider_call_id}"))
                if inbound.provider_call_id
                else str(uuid4())
            ),
        )
        if self.call_session_store is not None:
            if not inbound.provider_call_id:
                return await adapter.reject_inbound(
                    inbound, "Provider call ID is required for durable inbound call handling"
                )
            try:
                session, created, stored_status = await self.call_session_store.register_inbound(
                    session
                )
            except IdempotencyConflict as exc:
                logger.warning(
                    "inbound_call_idempotency_conflict",
                    extra={"provider": provider, "error_type": type(exc).__name__},
                )
                return await adapter.reject_inbound(inbound, "Inbound call could not be verified")
            if not created and stored_status in {"completed", "failed"}:
                return await adapter.reject_inbound(inbound, "This inbound call is already closed")
            if not created and stored_status == "answered":
                # Provider webhook retries should reuse the persisted call and its
                # billing reservation rather than acquiring a second reservation.
                session, response = await adapter.accept_inbound(inbound, session)
                await self.call_session_store.update_state(
                    session,
                    status="answered",
                    provider_call_id=session.provider_call_id,
                    answered_at=session.started_at,
                )
                return response
        try:
            entitlement = await self.subscriptions.authorize_voice(
                inbound.tenant_id, session.session_id, inbound.channel, Direction.INBOUND
            )
        except HTTPException as exc:
            if self.call_session_store is not None:
                await self.call_session_store.update_state(
                    session, status="failed", end_reason="billing_authorization_failed"
                )
            if exc.status_code in (402, 403):
                return await adapter.reject_inbound(inbound, str(exc.detail))
            raise
        session.recording_allowed = entitlement.recording_allowed
        session.max_call_seconds = entitlement.max_call_seconds
        session.started_at = datetime.now(UTC)
        try:
            await self.mark_answered(session)
        except LookupError as exc:
            await self.subscriptions.release_voice(inbound.tenant_id, session.session_id)
            return await adapter.reject_inbound(inbound, str(exc))
        try:
            session, response = await adapter.accept_inbound(inbound, session)
        except BaseException:
            await self.subscriptions.release_voice(inbound.tenant_id, session.session_id)
            if self.call_session_store is not None:
                await self.call_session_store.update_state(
                    session, status="failed", end_reason="provider_accept_failed"
                )
            raise
        if self.call_session_store is not None:
            await self.call_session_store.update_state(
                session,
                status="answered",
                provider_call_id=session.provider_call_id,
                answered_at=session.started_at,
            )
        session_started(session.channel, session.provider)
        return response

    async def _require_vertex_project(self, tenant_id: str) -> None:
        if not self.require_tenant_vertex_project:
            return
        if self.vertex_project_directory is None:
            raise HTTPException(status_code=503, detail="Tenant Vertex project storage is not configured")
        if await self.vertex_project_directory.get_ready_project_id(tenant_id) is None:
            raise HTTPException(status_code=503, detail="Tenant Vertex project is not ready")

    async def resolve_outbound_start(
        self,
        tenant_id: str,
        session_id: str,
        *,
        confirmed_started: bool,
        provider_call_id: str | None = None,
    ) -> str:
        if self.call_session_store is None:
            raise HTTPException(status_code=503, detail="Call status storage is not configured")
        try:
            outcome = await self.call_session_store.resolve_outbound_start(
                tenant_id,
                session_id,
                confirmed_started=confirmed_started,
                provider_call_id=provider_call_id,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Call not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if outcome == "confirmed_not_started":
            # Billing release is idempotent by session ID; the store accepts this
            # same decision again if the process dies between these operations.
            await self.subscriptions.release_voice(tenant_id, session_id)
        return outcome

    async def mark_answered(self, session: AcceptedSession) -> datetime:
        if session.tenant_id is None:
            return session.started_at
        canonical_answered_at = await self.subscriptions.mark_answered(
            session.tenant_id, session.session_id, session.started_at
        )
        if canonical_answered_at is not None:
            session.started_at = canonical_answered_at
        self.attach_session_persistence(session)
        if self.call_session_store is not None:
            await self.call_session_store.update_state(
                session,
                status="answered",
                provider_call_id=session.provider_call_id,
                answered_at=session.started_at,
            )
        session.metadata["call_status"] = "answered"
        return session.started_at

    def attach_session_persistence(self, session: AcceptedSession) -> None:
        """Attach the durable transcript checkpoint hook to a live session."""
        if self.call_session_store is None or session.tenant_id is None:
            return

        async def persist_segment(speaker: str, text: str) -> None:
            await self.call_session_store.append_transcript(
                session.session_id, session.tenant_id or "", speaker, text
            )

        session.metadata["persist_transcript_segment"] = persist_segment

    async def end_session(self, session: AcceptedSession, reason: str) -> None:
        if session.metadata.get("coordinator_session_ended"):
            return
        session.metadata["coordinator_session_ended"] = True
        ended_at = datetime.now(UTC)
        elapsed_seconds = max(
            0.0, (ended_at - _aware_utc(session.started_at)).total_seconds()
        )
        emit_limit_event = False
        try:
            if session.tenant_id:
                emit_limit_event = await self.subscriptions.settle_voice(
                    session.tenant_id,
                    session.session_id,
                    _billable_seconds(session.started_at, ended_at),
                    ended_at=ended_at,
                )
                if reason == "timeout":
                    emit_limit_event = (
                        await self.subscriptions.mark_duration_limit_event(
                            session.tenant_id, session.session_id
                        )
                    ) or emit_limit_event
        finally:
            # Setup failures release reservations; completed sessions settle them.
            # Billing policies must make settlement idempotent by session ID because
            # providers may deliver both a media disconnect and a hangup callback.
            session_ended(
                session.channel,
                session.provider,
                reason,
                duration_seconds=elapsed_seconds,
            )
            if emit_limit_event:
                call_duration_limit_event()
            if session.metadata.get("transcript_persistence_error"):
                logger.error(
                    "call_transcript_checkpoint_incomplete",
                    extra={"session_id": session.session_id, "tenant_id": session.tenant_id},
                )
            finalize_recording = session.metadata.get("finalize_recording")
            if (
                session.metadata.get("recording_pipeline_finished") is True
                and callable(finalize_recording)
            ):
                try:
                    # AudioBufferProcessor emits its final buffered audio only
                    # when stopped. Flush before persisting the session so its
                    # recording URI is included in the durable call record.
                    await finalize_recording()
                except Exception as exc:
                    logger.error(
                        "recording_finalization_failed",
                        extra={
                            "session_id": session.session_id,
                            "tenant_id": session.tenant_id,
                            "provider": session.provider,
                            "error_type": type(exc).__name__,
                        },
                    )
            if self.call_session_store is not None:
                try:
                    await self.call_session_store.update_state(
                        session,
                        status="completed",
                        provider_call_id=session.provider_call_id,
                        ended_at=ended_at,
                        end_reason=reason,
                    )
                except Exception as exc:
                    logger.error(
                        "call_lifecycle_persist_failed",
                        extra={"session_id": session.session_id, "error_type": type(exc).__name__},
                    )
            session.metadata["call_status"] = "completed"
            await self._enqueue_post_call(session, ended_at)

    async def _enqueue_post_call(self, session: AcceptedSession, ended_at: datetime) -> None:
        if not session.tenant_id:
            return
        if session.metadata.get("post_call_enqueued") is True:
            return
        if not session.post_call_agent_enabled or not self.post_call_required or self.post_call_dispatcher is None:
            session.metadata["post_call_status"] = "disabled"
            if self.call_session_store is not None:
                await self.call_session_store.mark_post_call_enqueued(
                    session.tenant_id, session.session_id
                )
            session.metadata["post_call_enqueued"] = True
            return
        transcript = session.metadata.get("post_call_transcript")
        if not isinstance(transcript, str) or not transcript.strip():
            post_call_missing_transcript(session.channel, session.provider)
            logger.warning(
                "post_call_missing_transcript",
                extra={
                    "session_id": session.session_id,
                    "tenant_id": session.tenant_id,
                    "channel": session.channel,
                    "provider": session.provider,
                },
            )
            return
        call = CompletedCall(
            session_id=session.session_id,
            tenant_id=session.tenant_id,
            api_key_id=session.api_key_id,
            provider=session.provider,
            channel=session.channel,
            direction=session.direction.value,
            started_at=session.started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            transcript=transcript,
            post_call_context=session.post_call_context,
            output_schema=session.post_call_output_schema,
            tools=tuple(session.post_call_tools or ()),
            time_zone=session.post_call_time_zone or "UTC",
            time_format=session.post_call_time_format or "%Y-%m-%dT%H:%M:%S%z",
            live_agent_context=(
                session.live_agent_context if session.include_live_agent_context else None
            ),
            recording_uri=session.metadata.get("recording_uri"),
        )
        try:
            # This writes a durable queue record only; model work happens in the worker.
            await self.post_call_dispatcher.enqueue(call)
            if self.call_session_store is not None:
                await self.call_session_store.mark_post_call_enqueued(
                    session.tenant_id, session.session_id
                )
            session.metadata["post_call_enqueued"] = True
            session.metadata.pop("post_call_transcript", None)
        except Exception as exc:
            logger.error(
                "post_call_enqueue_failed",
                extra={
                    "session_id": session.session_id,
                    "tenant_id": session.tenant_id,
                    "error_type": type(exc).__name__,
                },
            )
            if self.post_call_fallback_spool is None:
                post_call_enqueue_fallback_event("unavailable")
                return
            try:
                await self.post_call_fallback_spool.store(call)
            except Exception as spool_exc:
                post_call_enqueue_fallback_event("failed")
                logger.error(
                    "post_call_fallback_spool_failed",
                    extra={
                        "session_id": session.session_id,
                        "tenant_id": session.tenant_id,
                        "error_type": type(spool_exc).__name__,
                    },
                )
                return
            post_call_enqueue_fallback_event("saved")
            if self.call_session_store is not None:
                await self.call_session_store.mark_post_call_enqueued(
                    session.tenant_id, session.session_id
                )
            session.metadata["post_call_enqueued"] = True
            session.metadata.pop("post_call_transcript", None)

    def _require_post_call_ready(self) -> None:
        if self.post_call_required and not self.post_call_available:
            raise HTTPException(
                status_code=503,
                detail="Post-call analysis is enabled but its agent or durable queue is not configured",
            )

    def _adapter(self, provider: str) -> ChannelAdapter:
        adapter = self.adapters.get(provider)
        if adapter is None:
            raise HTTPException(status_code=503, detail=f"Channel provider '{provider}' is not configured")
        return adapter


@dataclass(frozen=True, slots=True)
class _OutboundCallData:
    provider: str
    channel: str
    tenant_id: str
    destination: str
    caller_id: str | None
    bot_config_id: str | None
    live_agent_instructions: str | None
    live_agent_context: dict[str, Any] | None
    post_call_context: str | None
    post_call_output_schema: dict[str, Any] | None
    post_call_tools: list[str] | None
    post_call_time_zone: str | None
    post_call_time_format: str | None
    include_live_agent_context: bool
    recording_allowed: bool
    session_id: str
    api_key_id: str | None = None


def _billable_seconds(started_at: datetime, ended_at: datetime) -> int:
    """Round any partial billable second up and tolerate legacy naive timestamps."""
    started_at = _aware_utc(started_at)
    ended_at = _aware_utc(ended_at)
    return max(0, ceil((ended_at - started_at).total_seconds()))


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
