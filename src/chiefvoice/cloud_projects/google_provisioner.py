from __future__ import annotations

import hashlib
import json
import logging
import time
from decimal import Decimal, InvalidOperation


logger = logging.getLogger(__name__)
CRM = "https://cloudresourcemanager.googleapis.com/v1"
BILLING = "https://cloudbilling.googleapis.com/v1"
SERVICE_USAGE = "https://serviceusage.googleapis.com/v1"
BUDGETS = "https://billingbudgets.googleapis.com/v1"
VERTEX_SERVICE_ID = "C7E2-9256-1C43"


class GoogleCloudTenantProjectProvisioner:
    """Creates one billed, Vertex-enabled project per tenant using platform ADC."""

    def __init__(
        self,
        *,
        parent: str,
        billing_account_id: str,
        runtime_service_account: str,
        additional_runtime_service_accounts: tuple[str, ...] = (),
        credentials_json: str | None = None,
        credentials_path: str | None = None,
        budget_api_project_id: str,
        monthly_vertex_spend_cap: str,
    ):
        self.parent = parent.strip("/")
        self.billing_account_id = billing_account_id.removeprefix("billingAccounts/")
        self.runtime_service_account = runtime_service_account
        self.runtime_service_accounts = tuple(
            dict.fromkeys((runtime_service_account, *additional_runtime_service_accounts))
        )
        self.budget_api_project_id = budget_api_project_id
        try:
            self.spend_cap = Decimal(monthly_vertex_spend_cap)
        except InvalidOperation as exc:
            raise ValueError("GCP_VERTEX_MONTHLY_SPEND_CAP must be a positive amount") from exc
        if self.spend_cap <= 0 or not self.parent or not self.billing_account_id:
            raise ValueError("GCP project parent, billing account, and positive spend cap are required")
        if not self.runtime_service_account or not self.budget_api_project_id:
            raise ValueError("GCP runtime service account and budget API project are required")
        if not (self.parent.startswith("folders/") or self.parent.startswith("organizations/")):
            raise ValueError("GCP_PROJECT_PARENT must be folders/<id> or organizations/<id>")

        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        scopes = ["https://www.googleapis.com/auth/cloud-platform"]
        if credentials_json:
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_info(
                json.loads(credentials_json), scopes=scopes
            )
        elif credentials_path:
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                credentials_path, scopes=scopes
            )
        else:
            credentials, _ = google.auth.default(scopes=scopes)
        self._session = AuthorizedSession(credentials)

    def provision(self, tenant_id: str, project_id: str, display_name: str) -> str:
        tenant_hash = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:16]
        self._ensure_project(project_id, tenant_hash)
        self._link_billing(project_id)
        project = self._request("GET", f"{CRM}/projects/{project_id}").json()
        project_number = str(project.get("projectNumber", ""))
        if not project_number:
            raise RuntimeError("Google Cloud did not return a project number")
        self._enable_service(project_number, "aiplatform.googleapis.com")
        self._grant_vertex_access(project_id)
        budget_name = self._ensure_vertex_spend_cap(project_id, tenant_hash)
        logger.info(
            "tenant_vertex_project_ready",
            extra={"tenant_id": tenant_id, "project_id": project_id},
        )
        return budget_name

    def _ensure_project(self, project_id: str, tenant_hash: str) -> None:
        response = self._session.get(f"{CRM}/projects/{project_id}", timeout=15)
        if response.status_code == 200:
            labels = response.json().get("labels", {})
            if labels.get("chiefvoice_tenant") != tenant_hash:
                raise RuntimeError("Google Cloud project ID is already owned by another resource")
            return
        if response.status_code != 404:
            self._raise(response)
        parent_kind, parent_id = self.parent.split("/", maxsplit=1)
        body = {
            "projectId": project_id,
            "name": f"Chiefvoice tenant {tenant_hash}",
            "parent": {"type": parent_kind[:-1], "id": parent_id},
            "labels": {"chiefvoice_tenant": tenant_hash},
        }
        response = self._session.post(f"{CRM}/projects", json=body, timeout=30)
        if response.status_code == 409:
            # A create may have succeeded even if its response was lost. Verify ownership.
            response = self._session.get(f"{CRM}/projects/{project_id}", timeout=15)
            self._raise(response)
            if response.json().get("labels", {}).get("chiefvoice_tenant") != tenant_hash:
                raise RuntimeError("Google Cloud project ID collision")
            return
        self._raise(response)
        operation_name = response.json().get("name")
        if not operation_name:
            raise RuntimeError("Google Cloud project creation returned no operation name")
        self._wait_operation(f"{CRM}/{operation_name}")

    def _link_billing(self, project_id: str) -> None:
        self._request(
            "PUT",
            f"{BILLING}/projects/{project_id}/billingInfo",
            json={"billingAccountName": f"billingAccounts/{self.billing_account_id}"},
        )

    def _enable_service(self, project_number: str, service: str) -> None:
        response = self._request(
            "POST",
            f"{SERVICE_USAGE}/projects/{project_number}/services/{service}:enable",
            json={},
        )
        operation_name = response.json().get("name")
        if operation_name:
            self._wait_operation(f"{SERVICE_USAGE}/{operation_name}")

    def _grant_vertex_access(self, project_id: str) -> None:
        policy_url = f"{CRM}/projects/{project_id}:getIamPolicy"
        policy_response = self._request("POST", policy_url, json={"options": {"requestedPolicyVersion": 3}})
        policy = policy_response.json()
        bindings = policy.setdefault("bindings", [])
        binding = next((item for item in bindings if item.get("role") == "roles/aiplatform.user"), None)
        if binding is None:
            binding = {"role": "roles/aiplatform.user", "members": []}
            bindings.append(binding)
        changed = False
        for service_account in self.runtime_service_accounts:
            member = f"serviceAccount:{service_account}"
            if member not in binding["members"]:
                binding["members"].append(member)
                changed = True
        if changed:
            self._request(
                "POST",
                f"{CRM}/projects/{project_id}:setIamPolicy",
                json={"policy": policy},
            )

    def _ensure_vertex_spend_cap(self, project_id: str, tenant_hash: str) -> str:
        display_name = f"Chiefvoice Vertex cap {tenant_hash}"
        base_url = f"{BUDGETS}/billingAccounts/{self.billing_account_id}/budgets"
        page_token = None
        while True:
            params = {"pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            response = self._request(
                "GET",
                base_url,
                params=params,
                headers={"x-goog-user-project": self.budget_api_project_id},
            ).json()
            for budget in response.get("budgets", []):
                budget_filter = budget.get("budgetFilter", {})
                if (
                    budget.get("displayName") == display_name
                    and budget_filter.get("projects") == [f"projects/{project_id}"]
                    and budget_filter.get("services") == [f"services/{VERTEX_SERVICE_ID}"]
                ):
                    return budget["name"]
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        units = int(self.spend_cap)
        nanos = int((self.spend_cap - Decimal(units)) * Decimal(1_000_000_000))
        body = {
            "displayName": display_name,
            "budgetFilter": {
                "projects": [f"projects/{project_id}"],
                "services": [f"services/{VERTEX_SERVICE_ID}"],
                "creditTypesTreatment": "EXCLUDE_ALL_CREDITS",
                "calendarPeriod": "MONTH",
            },
            "amount": {"specifiedAmount": {"units": str(units), "nanos": nanos}},
            "thresholdRules": [
                {"thresholdPercent": 0.5, "spendBasis": "CURRENT_SPEND"},
                {"thresholdPercent": 0.8, "spendBasis": "CURRENT_SPEND"},
                {"thresholdPercent": 1.0, "spendBasis": "CURRENT_SPEND"},
            ],
            "notificationsRule": {"enableProjectLevelRecipients": True},
            "spendCap": {"inputState": "CONFIGURED"},
        }
        response = self._request(
            "POST",
            base_url,
            json=body,
            headers={"x-goog-user-project": self.budget_api_project_id},
        ).json()
        name = response.get("name")
        if not name:
            raise RuntimeError("Google Cloud budget API returned no budget name")
        return name

    def _wait_operation(self, url: str, *, timeout_seconds: int = 300) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            response = self._request("GET", url).json()
            if response.get("done"):
                if response.get("error"):
                    raise RuntimeError("Google Cloud asynchronous operation failed")
                return
            time.sleep(2)
        raise TimeoutError("Google Cloud provisioning operation timed out")

    def _request(self, method: str, url: str, **kwargs):
        response = self._session.request(method, url, timeout=30, **kwargs)
        self._raise(response)
        return response

    @staticmethod
    def _raise(response) -> None:
        if response.status_code >= 400:
            raise RuntimeError(f"Google Cloud API request failed with HTTP {response.status_code}")
