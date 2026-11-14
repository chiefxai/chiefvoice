from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TenantVertexProject:
    tenant_id: str
    project_id: str
    status: str
    attempts: int
    last_error_type: str | None
    budget_name: str | None
    created_at: datetime
    updated_at: datetime


class TenantVertexProjectDirectory(Protocol):
    async def request_provisioning(self, tenant_id: str, display_name: str) -> dict: ...
    async def get(self, tenant_id: str) -> TenantVertexProject | None: ...
    async def get_ready_project_id(self, tenant_id: str) -> str | None: ...
    async def retry(self, tenant_id: str) -> dict: ...
    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
