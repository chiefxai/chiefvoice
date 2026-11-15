from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from chiefvoice.billing import BillingMode


@dataclass(frozen=True, slots=True)
class TenantAccount:
    tenant_id: str
    display_name: str
    billing_mode: BillingMode
    created_by: str
    created_at: datetime


class AccountStore(Protocol):
    async def create(self, account: TenantAccount) -> bool:
        """Create once; return False when the tenant account already exists."""
        ...

    async def get(self, tenant_id: str) -> TenantAccount | None: ...
    async def update_display_name(self, tenant_id: str, display_name: str) -> bool: ...
    async def list(self, *, limit: int = 100, offset: int = 0) -> list[TenantAccount]: ...
    async def get_billing_mode(self, tenant_id: str) -> BillingMode | None: ...
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...


def new_tenant_account(
    tenant_id: str, display_name: str, billing_mode: BillingMode, created_by: str
) -> TenantAccount:
    return TenantAccount(
        tenant_id=tenant_id,
        display_name=display_name,
        billing_mode=billing_mode,
        created_by=created_by,
        created_at=datetime.now(UTC),
    )
