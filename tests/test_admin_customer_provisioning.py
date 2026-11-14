import pytest
from fastapi import HTTPException

from chiefvoice.accounts.admin_routes import (
    CUSTOMER_API_KEY_SCOPES,
    CreateCustomerApiKeyBody,
    CreateCustomerBody,
    _platform_admin,
)
from chiefvoice.identity.auth import Principal


def test_customer_api_key_scopes_are_limited_to_tenant_operations():
    body = CreateCustomerBody(
        display_name="  Acme  ",
        billing_mode="prepaid",
        api_key_scopes=["calls:write", "agents:read"],
    )
    assert body.display_name == "Acme"
    assert set(body.api_key_scopes) == {"calls:write", "agents:read"}
    assert "platform:customers:write" not in CUSTOMER_API_KEY_SCOPES
    with pytest.raises(ValueError, match="Unsupported customer API key scopes"):
        CreateCustomerApiKeyBody(name="bad", scopes=["calls:reconcile"])


@pytest.mark.asyncio
async def test_platform_customer_provisioning_requires_allowlisted_oidc_subject_and_scope(
    monkeypatch,
):
    monkeypatch.setattr(
        "chiefvoice.accounts.admin_routes.settings",
        type("Settings", (), {"platform_admin_subjects": ("operator-1",)})(),
    )
    dependency = _platform_admin("platform:customers:write")

    allowed = Principal(
        subject="operator-1",
        tenant_id="platform-tenant",
        scopes=frozenset({"platform:customers:write"}),
        auth_method="oidc",
    )
    assert await dependency(principal=allowed) == allowed

    wrong_subject = Principal(
        subject="operator-2",
        tenant_id="platform-tenant",
        scopes=frozenset({"platform:customers:write"}),
        auth_method="oidc",
    )
    with pytest.raises(HTTPException) as exc:
        await dependency(principal=wrong_subject)
    assert exc.value.status_code == 403

    api_key = Principal(
        subject="api_key:one",
        tenant_id="customer-1",
        scopes=frozenset({"platform:customers:write"}),
        auth_method="api_key",
    )
    with pytest.raises(HTTPException) as exc:
        await dependency(principal=api_key)
    assert exc.value.status_code == 403

    missing_scope = Principal(
        subject="operator-1",
        tenant_id="platform-tenant",
        scopes=frozenset(),
        auth_method="oidc",
    )
    with pytest.raises(HTTPException) as exc:
        await dependency(principal=missing_scope)
    assert exc.value.status_code == 403
