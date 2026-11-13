from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ProviderPhoneNumber:
    number: str
    inbound_enabled: bool = True
    outbound_enabled: bool = True


@dataclass(frozen=True, slots=True)
class ProviderIntegration:
    provider: str
    tenant_id: str
    credentials: dict[str, str]
    settings: dict[str, Any]
    phone_numbers: tuple[ProviderPhoneNumber, ...]


class ProviderIntegrationStore(Protocol):
    async def get(self, provider: str, tenant_id: str) -> ProviderIntegration | None: ...
    async def get_for_inbound_number(self, provider: str, number: str) -> ProviderIntegration | None: ...
    async def save(self, integration: ProviderIntegration) -> None: ...
    async def delete(self, provider: str, tenant_id: str) -> None: ...
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
