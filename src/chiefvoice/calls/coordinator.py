import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException, Request, Response

from chiefvoice.agents.call.configs.profiles import AgentProfileDirectory
from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.agents.post_call.fallback_spool import PostCallFallbackSpool
from chiefvoice.agents.post_call.queue import PostCallDispatcher
from chiefvoice.billing import SubscriptionAuthorizer
from chiefvoice.channels.base import ChannelAdapter, InboundCall, OutboundCall
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
    caller_id: str | None = None
    bot_config_id: str | None = None
    live_agent_context: dict[str, Any] | None = None
    post_call_context: str | None = None
    post_call_output_schema: dict[str, Any] | None = None
    post_call_tools: list[str] | None = None
    post_call_time_zone: str | None = None
    post_call_time_format: str | None = None
    include_live_agent_context: bool = False
    recording_allowed: bool = False


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
    ):
        self.adapters = adapters
        self.subscriptions = subscriptions
        self.post_call_dispatcher = post_call_dispatcher
        self.post_call_required = post_call_required
        self.post_call_available = post_call_available
        self.agent_profile_directory = agent_profile_directory
        self.post_call_fallback_spool = post_call_fallback_spool

    async def start_outbound(self, command: OutboundCallCommand) -> AcceptedSession:
        self._require_post_call_ready()
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
        session = AcceptedSession(
            channel=command.channel,
            provider=command.provider,
            direction=Direction.OUTBOUND,
            caller=command.caller_id,
            callee=command.destination,
            tenant_id=command.tenant_id,
            bot_config_id=command.bot_config_id,
            live_agent_context=command.live_agent_context,
            post_call_context=command.post_call_context,
            post_call_output_schema=command.post_call_output_schema,
            post_call_tools=command.post_call_tools,
            post_call_time_zone=command.post_call_time_zone,
            post_call_time_format=command.post_call_time_format,
            include_live_agent_context=command.include_live_agent_context,
        )
        entitlement = await self.subscriptions.authorize_voice(
            command.tenant_id, session.session_id, command.channel, Direction.OUTBOUND
        )
        call: OutboundCall = _OutboundCallData(
            provider=command.provider,
            channel=command.channel,
            tenant_id=command.tenant_id,
            destination=command.destination,
            caller_id=command.caller_id,
            bot_config_id=command.bot_config_id,
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
        try:
            session = await adapter.initiate_outbound(call, session)
        except BaseException:
            await self.subscriptions.release_voice(command.tenant_id, session.session_id)
            raise
        session_started(session.channel, session.provider)
        return session

    async def handle_inbound(self, provider: str, request: Request) -> Response:
        adapter = self._adapter(provider)
        inbound = await adapter.parse_inbound(request)
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
        try:
            entitlement = await self.subscriptions.authorize_voice(
                inbound.tenant_id, session.session_id, inbound.channel, Direction.INBOUND
            )
        except HTTPException as exc:
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
            raise
        session_started(session.channel, session.provider)
        return response

    async def mark_answered(self, session: AcceptedSession) -> datetime:
        if session.tenant_id is None:
            return session.started_at
        canonical_answered_at = await self.subscriptions.mark_answered(
            session.tenant_id, session.session_id, session.started_at
        )
        if canonical_answered_at is not None:
            session.started_at = canonical_answered_at
        return session.started_at

    async def end_session(self, session: AcceptedSession, reason: str) -> None:
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
            await self._enqueue_post_call(session, ended_at)

    async def _enqueue_post_call(self, session: AcceptedSession, ended_at: datetime) -> None:
        if not self.post_call_required or self.post_call_dispatcher is None or not session.tenant_id:
            return
        if session.metadata.get("post_call_enqueued") is True:
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
        )
        try:
            # This writes a durable queue record only; model work happens in the worker.
            await self.post_call_dispatcher.enqueue(call)
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
    live_agent_context: dict[str, Any] | None
    post_call_context: str | None
    post_call_output_schema: dict[str, Any] | None
    post_call_tools: list[str] | None
    post_call_time_zone: str | None
    post_call_time_format: str | None
    include_live_agent_context: bool
    recording_allowed: bool
    session_id: str


def _billable_seconds(started_at: datetime, ended_at: datetime) -> int:
    """Round any partial billable second up and tolerate legacy naive timestamps."""
    started_at = _aware_utc(started_at)
    ended_at = _aware_utc(ended_at)
    return max(0, ceil((ended_at - started_at).total_seconds()))


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
