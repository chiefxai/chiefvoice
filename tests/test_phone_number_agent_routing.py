from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import State

from chiefvoice.channels.integration_routes import (
    PhoneNumberBody,
    ProviderIntegrationBody,
    put_integration,
)
from chiefvoice.identity.auth import Principal


class _IntegrationStore:
    def __init__(self):
        self.saved = None

    async def save(self, integration):
        self.saved = integration


class _ProfileDirectory:
    def __init__(self, available):
        self.available = set(available)

    async def get(self, tenant_id, agent_id):
        if tenant_id == "customer-a" and agent_id in self.available:
            return object()
        return None


def _request(profile_directory):
    integration_store = _IntegrationStore()
    state = State(
        {
            "provider_integration_store": integration_store,
            "agent_profile_directory": profile_directory,
        }
    )
    return SimpleNamespace(app=SimpleNamespace(state=state)), integration_store


def _principal():
    return Principal(
        subject="api_key:key-a",
        tenant_id="customer-a",
        scopes=frozenset({"integrations:write"}),
        auth_method="api_key",
    )


@pytest.mark.asyncio
async def test_provider_numbers_can_map_distinct_tenant_agents():
    request, store = _request(_ProfileDirectory({"agent-1", "agent-2"}))
    body = ProviderIntegrationBody(
        credentials={"auth_id": "account", "auth_token": "secret"},
        settings={"default_caller_id": "+15555550101"},
        phone_numbers=[
            PhoneNumberBody(number="+15555550101", agent_id="agent-1"),
            PhoneNumberBody(number="+15555550102", agent_id="agent-2"),
        ],
    )

    result = await put_integration("vobiz", body, request, _principal())

    assert result == {"status": "configured", "provider": "vobiz"}
    assert [number.agent_id for number in store.saved.phone_numbers] == ["agent-1", "agent-2"]
    assert store.saved.tenant_id == "customer-a"


@pytest.mark.asyncio
async def test_provider_number_cannot_reference_another_tenants_agent():
    request, store = _request(_ProfileDirectory(set()))
    body = ProviderIntegrationBody(
        credentials={"auth_id": "account", "auth_token": "secret"},
        settings={"default_caller_id": "+15555550101"},
        phone_numbers=[PhoneNumberBody(number="+15555550101", agent_id="agent-other")],
    )

    with pytest.raises(HTTPException) as exc:
        await put_integration("vobiz", body, request, _principal())
    assert exc.value.status_code == 422
    assert store.saved is None
