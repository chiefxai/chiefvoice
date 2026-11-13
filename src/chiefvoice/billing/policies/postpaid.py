from datetime import datetime
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

    async def accrue_postpaid_voice_at(
        self, tenant_id: str, session_id: str, billable_seconds: int, ended_at: datetime
    ) -> None: ...

    async def mark_postpaid_voice_answered(
        self, tenant_id: str, session_id: str, answered_at: datetime
    ) -> datetime | None: ...

    async def mark_postpaid_voice_duration_limit_event(
        self, tenant_id: str, session_id: str
    ) -> bool: ...


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

    async def settle_voice_session_at(
        self, tenant_id: str, session_id: str, billable_seconds: int, ended_at: datetime
    ) -> bool:
        method = getattr(self.ledger, "accrue_postpaid_voice_at", None)
        if method is None:
            await self.ledger.accrue_postpaid_voice(tenant_id, session_id, billable_seconds)
            return False
        else:
            return bool(await method(tenant_id, session_id, billable_seconds, ended_at))

    async def mark_voice_session_answered(
        self, tenant_id: str, session_id: str, answered_at: datetime
    ) -> datetime | None:
        method = getattr(self.ledger, "mark_postpaid_voice_answered", None)
        if method is not None:
            return await method(tenant_id, session_id, answered_at)
        return None

    async def mark_duration_limit_event(self, tenant_id: str, session_id: str) -> bool:
        method = getattr(self.ledger, "mark_postpaid_voice_duration_limit_event", None)
        return await method(tenant_id, session_id) if method is not None else False
