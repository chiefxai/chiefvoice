from typing import Protocol, runtime_checkable

from fastapi import Request, Response
from pipecat.transports.base_transport import BaseTransport

from chiefvoice.sessions import AcceptedSession, Direction


class InboundCall(Protocol):
    """Provider-verified call data, including a tenant resolved from provider config."""

    provider: str
    channel: str
    tenant_id: str
    caller: str | None
    callee: str | None
    provider_call_id: str | None
    bot_config_id: str | None


class OutboundCall(Protocol):
    provider: str
    channel: str
    tenant_id: str
    destination: str
    caller_id: str | None
    bot_config_id: str | None
    recording_allowed: bool
    session_id: str


@runtime_checkable
class ChannelAdapter(Protocol):
    name: str
    provider: str

    async def parse_inbound(self, request: Request) -> InboundCall:
        """Verify provider signature and resolve its account/number to a tenant."""
        ...

    async def accept_inbound(
        self, call: InboundCall, session: AcceptedSession
    ) -> tuple[AcceptedSession, Response]:
        """Create an inbound session and return the provider's webhook response."""
        ...

    async def reject_inbound(self, call: InboundCall, reason: str) -> Response:
        """Return a provider-specific reject response for a verified inbound call."""
        ...

    async def initiate_outbound(self, call: OutboundCall, session: AcceptedSession) -> AcceptedSession:
        """Start an outbound call through the provider's control API."""
        ...

    async def build_transport(self, session: AcceptedSession, connection: object) -> BaseTransport: ...

    async def hangup(self, session: AcceptedSession, reason: str | None = None) -> None: ...

    async def transfer(self, session: AcceptedSession, destination: str) -> None: ...
