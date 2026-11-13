from typing import Protocol

from chiefvoice.billing.models import BillingMode, VoiceEntitlement
from chiefvoice.sessions import Direction


class VoiceBillingPolicy(Protocol):
    """Reservation and settlement contract for one tenant billing mode.

    Implementations must make reserve, release, and settle idempotent by
    ``session_id``. Settlement finalizes usage and closes the reservation in
    the same durable operation so duplicate end events cannot double-charge
    or leave concurrency/balance holds behind.
    """

    mode: BillingMode

    async def reserve_voice_session(
        self, tenant_id: str, session_id: str, channel: str, direction: Direction
    ) -> VoiceEntitlement | None: ...

    async def release_voice_session(self, tenant_id: str, session_id: str) -> None: ...

    async def settle_voice_session(
        self, tenant_id: str, session_id: str, billable_seconds: int
    ) -> None: ...
