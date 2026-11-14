from chiefvoice.billing.models import BillingMode, VoiceEntitlement
from chiefvoice.calls.coordinator import CallCoordinator, OutboundCallCommand
from chiefvoice.sessions import AcceptedSession, Direction


class _Adapter:
    name = "vobiz"
    provider = "vobiz"

    async def initiate_outbound(self, call, session):
        self.call = call
        self.session = session
        return session


class _Subscriptions:
    async def authorize_voice(self, tenant_id, session_id, channel, direction):
        return VoiceEntitlement(
            tenant_id=tenant_id,
            billing_mode=BillingMode.PREPAID,
            can_use_voice=True,
            allowed_channels=frozenset({channel}),
            max_concurrent_sessions=1,
            recording_allowed=False,
        )

    async def release_voice(self, tenant_id, session_id):
        pass


class _AgentProfiles:
    async def get(self, tenant_id, profile_id):
        return object()


async def test_outbound_context_survives_coordinator_to_channel_adapter():
    adapter = _Adapter()
    coordinator = CallCoordinator(
        {"vobiz": adapter},
        _Subscriptions(),
        post_call_required=False,
        agent_profile_directory=_AgentProfiles(),
    )
    output_schema = {
        "type": "object",
        "properties": {"confirmed": {"type": "boolean"}},
        "required": ["confirmed"],
    }
    command = OutboundCallCommand(
        provider="vobiz",
        channel="phone",
        tenant_id="tenant-1",
        destination="+15551234567",
        bot_config_id="agent-1",
        live_agent_context={"patient_name": "Alex"},
        post_call_context="Extract confirmation status.",
        post_call_output_schema=output_schema,
        post_call_tools=["calculator", "time"],
        post_call_time_zone="Asia/Kolkata",
        post_call_time_format="%Y-%m-%dT%H:%M:%S%z",
        include_live_agent_context=True,
    )

    session: AcceptedSession = await coordinator.start_outbound(command)

    assert session.direction is Direction.OUTBOUND
    assert adapter.call.live_agent_context == {"patient_name": "Alex"}
    assert adapter.session.post_call_context == "Extract confirmation status."
    assert adapter.session.post_call_output_schema == output_schema
    assert adapter.call.post_call_tools == ["calculator", "time"]
    assert adapter.session.post_call_time_zone == "Asia/Kolkata"
    assert adapter.session.post_call_time_format == "%Y-%m-%dT%H:%M:%S%z"
    assert adapter.session.include_live_agent_context is True
