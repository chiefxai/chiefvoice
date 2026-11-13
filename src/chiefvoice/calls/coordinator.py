import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException, Request, Response

from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.agents.post_call.queue import PostCallDispatcher
from chiefvoice.agents.call.configs.profiles import AgentProfileDirectory
from chiefvoice.billing import SubscriptionAuthorizer
from chiefvoice.channels.base import ChannelAdapter, InboundCall, OutboundCall
from chiefvoice.observability.setup import (
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
    ):
        self.adapters = adapters
        self.subscriptions = subscriptions
        self.post_call_dispatcher = post_call_dispatcher
        self.post_call_required = post_call_required
        self.post_call_available = post_call_available
        self.agent_profile_directory = agent_profile_directory

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
            recording_allowed=command.recording_allowed and entitlement.recording_allowed,
            session_id=session.session_id,
        )
        session.recording_allowed = call.recording_allowed
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
        try:
            session, response = await adapter.accept_inbound(inbound, session)
        except BaseException:
            await self.subscriptions.release_voice(inbound.tenant_id, session.session_id)
            raise
        session_started(session.channel, session.provider)
        return response

    async def end_session(self, session: AcceptedSession, reason: str) -> None:
        ended_at = datetime.now(UTC)
        try:
            if session.tenant_id:
                await self.subscriptions.settle_voice(
                    session.tenant_id,
                    session.session_id,
                    _billable_seconds(session.started_at, ended_at),
                )
        finally:
            # Setup failures release reservations; completed sessions settle them.
            # Billing policies must make settlement idempotent by session ID because
            # providers may deliver both a media disconnect and a hangup callback.
            session_ended(session.channel, session.provider, reason)
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
        )
        try:
            # This writes a durable queue record only; model work happens in the worker.
            await self.post_call_dispatcher.enqueue(call)
            session.metadata["post_call_enqueued"] = True
            session.metadata.pop("post_call_transcript", None)
        except Exception as exc:
            # Do not let a post-call queue outage interfere with call teardown.
            logger.error(
                "post_call_enqueue_failed",
                extra={
                    "session_id": session.session_id,
                    "tenant_id": session.tenant_id,
                    "error_type": type(exc).__name__,
                },
            )

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
    recording_allowed: bool
    session_id: str


def _billable_seconds(started_at: datetime, ended_at: datetime) -> int:
    """Round any partial billable second up and tolerate legacy naive timestamps."""
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=UTC)
    return max(0, ceil((ended_at - started_at).total_seconds()))
