from datetime import datetime
from typing import Protocol

from fastapi import HTTPException

from chiefvoice.billing.models import BillingMode, VoiceEntitlement
from chiefvoice.billing.policies import BillingPolicyRegistry
from chiefvoice.sessions import Direction


class BillingAccountDirectory(Protocol):
    async def get_billing_mode(self, tenant_id: str) -> BillingMode | None: ...


class SubscriptionAuthorizer:
    """Route call admission and usage settlement through the tenant's billing policy."""

    def __init__(
        self,
        billing_accounts: BillingAccountDirectory,
        policies: BillingPolicyRegistry,
    ):
        self.billing_accounts = billing_accounts
        self.policies = policies

    async def authorize_voice(
        self, tenant_id: str, session_id: str, channel: str, direction: Direction
    ) -> VoiceEntitlement:
        billing_mode = await self.billing_accounts.get_billing_mode(tenant_id)
        if billing_mode is None:
            raise HTTPException(status_code=402, detail="A billing account is required")
        policy = self.policies.get(billing_mode)
        entitlement = await policy.reserve_voice_session(
            tenant_id, session_id, channel, direction
        )
        if entitlement is None:
            raise HTTPException(status_code=402, detail="An active voice subscription is required")
        if entitlement.billing_mode != billing_mode:
            await policy.release_voice_session(tenant_id, session_id)
            raise HTTPException(status_code=503, detail="Billing policy returned an inconsistent mode")
        if not entitlement.can_use_voice:
            await policy.release_voice_session(tenant_id, session_id)
            raise HTTPException(status_code=402, detail="An active voice subscription is required")
        if channel not in entitlement.allowed_channels:
            await policy.release_voice_session(tenant_id, session_id)
            raise HTTPException(status_code=403, detail="The subscription does not include this channel")
        return entitlement

    async def release_voice(self, tenant_id: str, session_id: str) -> None:
        await self.policies.release_voice_session(tenant_id, session_id)

    async def settle_voice(
        self,
        tenant_id: str,
        session_id: str,
        billable_seconds: int,
        ended_at: datetime | None = None,
    ) -> bool:
        seconds = max(0, billable_seconds)
        if ended_at is None:
            await self.policies.settle_voice_session(tenant_id, session_id, seconds)
            return False
        else:
            return await self.policies.settle_voice_session_at(
                tenant_id, session_id, seconds, ended_at
            )

    async def mark_answered(
        self, tenant_id: str, session_id: str, answered_at: datetime
    ) -> datetime | None:
        return await self.policies.mark_voice_session_answered(
            tenant_id, session_id, answered_at
        )

    async def mark_duration_limit_event(self, tenant_id: str, session_id: str) -> bool:
        return await self.policies.mark_duration_limit_event(tenant_id, session_id)
