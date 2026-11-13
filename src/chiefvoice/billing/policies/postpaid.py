from typing import Protocol

from chiefvoice.billing.models import BillingMode, VoiceEntitlement
from chiefvoice.sessions import Direction


class PostpaidLedger(Protocol):
    async def authorize_postpaid_voice(
        self, tenant_id: str, session_id: str, channel: str, direction: Direction
    ) -> VoiceEntitlement | None: ...

    async def release_postpaid_voice(self, tenant_id: str, session_id: str) -> None: ...

    async def accrue_postpaid_voice(
        self, tenant_id: str, session_id: str, billable_seconds: int
    ) -> None:
        """Idempotently accrue actual usage and close the credit reservation."""
        ...


class PostpaidBillingPolicy:
    """Postpaid credit admission, reservation, and usage accrual contract."""

    mode = BillingMode.POSTPAID

    def __init__(self, ledger: PostpaidLedger):
        self.ledger = ledger

    async def reserve_voice_session(
        self, tenant_id: str, session_id: str, channel: str, direction: Direction
    ) -> VoiceEntitlement | None:
        return await self.ledger.authorize_postpaid_voice(
            tenant_id, session_id, channel, direction
        )

    async def release_voice_session(self, tenant_id: str, session_id: str) -> None:
        await self.ledger.release_postpaid_voice(tenant_id, session_id)

    async def settle_voice_session(
        self, tenant_id: str, session_id: str, billable_seconds: int
    ) -> None:
        await self.ledger.accrue_postpaid_voice(tenant_id, session_id, billable_seconds)
