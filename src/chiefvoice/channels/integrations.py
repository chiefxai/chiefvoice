from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ProviderPhoneNumber:
    number: str
    inbound_enabled: bool = True
    outbound_enabled: bool = True
    agent_id: str | None = None
    standard_channels: int = 1
    unlimited_channels: int = 0


@dataclass(frozen=True, slots=True)
class RegisteredPhoneNumber:
    provider: str
    tenant_id: str
    number: str
    inbound_enabled: bool
    outbound_enabled: bool
    agent_id: str | None = None
    standard_channels: int = 1
    unlimited_channels: int = 0


@dataclass(frozen=True, slots=True)
class ProviderIntegration:
    provider: str
    tenant_id: str
    credentials: dict[str, str]
    settings: dict[str, Any]
    phone_numbers: tuple[ProviderPhoneNumber, ...]


class ProviderIntegrationStore(Protocol):
    async def get(self, provider: str, tenant_id: str) -> ProviderIntegration | None: ...
    async def get_for_phone_number(self, provider: str, number: str) -> ProviderIntegration | None: ...
    async def get_for_inbound_number(
        self, provider: str, number: str
    ) -> ProviderIntegration | None: ...
    async def save(self, integration: ProviderIntegration) -> None: ...
    async def delete(self, provider: str, tenant_id: str) -> None: ...
    async def list_phone_numbers(self) -> list[RegisteredPhoneNumber]: ...
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
