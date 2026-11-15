from datetime import datetime
from typing import Protocol

from chiefvoice.billing.models import BillingMode, VoiceEntitlement
from chiefvoice.sessions import Direction


class PrepaidLedger(Protocol):
    async def reserve_prepaid_voice(
        self, tenant_id: str, session_id: str, channel: str, direction: Direction,
        api_key_id: str | None = None,
    ) -> VoiceEntitlement | None: ...

    async def release_prepaid_voice(self, tenant_id: str, session_id: str) -> None: ...

    async def settle_prepaid_voice(
        self, tenant_id: str, session_id: str, billable_seconds: int
    ) -> None:
        """Idempotently debit actual usage and close/refund the reservation."""
        ...

    async def settle_prepaid_voice_at(
        self, tenant_id: str, session_id: str, billable_seconds: int, ended_at: datetime
    ) -> None: ...

    async def mark_prepaid_voice_answered(
        self, tenant_id: str, session_id: str, answered_at: datetime
    ) -> datetime | None: ...

    async def mark_prepaid_voice_duration_limit_event(
        self, tenant_id: str, session_id: str
    ) -> bool: ...


class PrepaidBillingPolicy:
    """Prepaid balance reservation, refund, and usage debit contract."""

    mode = BillingMode.PREPAID

    def __init__(self, ledger: PrepaidLedger):
        self.ledger = ledger

    async def reserve_voice_session(
        self, tenant_id: str, session_id: str, channel: str, direction: Direction,
        api_key_id: str | None = None,
    ) -> VoiceEntitlement | None:
        if api_key_id is None:
            return await self.ledger.reserve_prepaid_voice(
                tenant_id, session_id, channel, direction
            )
        return await self.ledger.reserve_prepaid_voice(
            tenant_id, session_id, channel, direction, api_key_id
        )

    async def release_voice_session(self, tenant_id: str, session_id: str) -> None:
        await self.ledger.release_prepaid_voice(tenant_id, session_id)

    async def settle_voice_session(
        self, tenant_id: str, session_id: str, billable_seconds: int
    ) -> None:
        await self.ledger.settle_prepaid_voice(tenant_id, session_id, billable_seconds)

    async def settle_voice_session_at(
        self, tenant_id: str, session_id: str, billable_seconds: int, ended_at: datetime
    ) -> bool:
        method = getattr(self.ledger, "settle_prepaid_voice_at", None)
        if method is None:
            await self.ledger.settle_prepaid_voice(tenant_id, session_id, billable_seconds)
            return False
        else:
            return bool(await method(tenant_id, session_id, billable_seconds, ended_at))

    async def mark_voice_session_answered(
        self, tenant_id: str, session_id: str, answered_at: datetime
    ) -> datetime | None:
        method = getattr(self.ledger, "mark_prepaid_voice_answered", None)
        if method is not None:
            return await method(tenant_id, session_id, answered_at)
        return None

    async def mark_duration_limit_event(self, tenant_id: str, session_id: str) -> bool:
        method = getattr(self.ledger, "mark_prepaid_voice_duration_limit_event", None)
        return await method(tenant_id, session_id) if method is not None else False
