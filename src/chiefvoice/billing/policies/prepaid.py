from typing import Protocol

from chiefvoice.billing.models import BillingMode, VoiceEntitlement
from chiefvoice.billing.policies.base import VoiceBillingPolicy
from chiefvoice.sessions import Direction


class PrepaidLedger(Protocol):
    async def reserve_prepaid_voice(
        self, tenant_id: str, session_id: str, channel: str, direction: Direction
    ) -> VoiceEntitlement | None: ...

    async def release_prepaid_voice(self, tenant_id: str, session_id: str) -> None: ...

    async def settle_prepaid_voice(
        self, tenant_id: str, session_id: str, billable_seconds: int
    ) -> None:
        """Idempotently debit actual usage and close/refund the reservation."""
        ...


class PrepaidBillingPolicy:
    """Prepaid balance reservation, refund, and usage debit contract."""

    mode = BillingMode.PREPAID

    def __init__(self, ledger: PrepaidLedger):
        self.ledger = ledger

    async def reserve_voice_session(
        self, tenant_id: str, session_id: str, channel: str, direction: Direction
    ) -> VoiceEntitlement | None:
        return await self.ledger.reserve_prepaid_voice(
            tenant_id, session_id, channel, direction
        )

    async def release_voice_session(self, tenant_id: str, session_id: str) -> None:
        await self.ledger.release_prepaid_voice(tenant_id, session_id)

    async def settle_voice_session(
        self, tenant_id: str, session_id: str, billable_seconds: int
    ) -> None:
        await self.ledger.settle_prepaid_voice(tenant_id, session_id, billable_seconds)
