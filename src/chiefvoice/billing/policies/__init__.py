from collections.abc import Sequence

from chiefvoice.billing.models import BillingMode
from chiefvoice.billing.policies.base import VoiceBillingPolicy


class BillingPolicyRegistry:
    """Select admission by account mode; settle/release against idempotent ledgers."""

    def __init__(self, policies: Sequence[VoiceBillingPolicy]):
        self._policies = {policy.mode: policy for policy in policies}
        if len(self._policies) != len(policies):
            raise ValueError("Only one billing policy may be registered per billing mode")

    def get(self, mode: BillingMode) -> VoiceBillingPolicy:
        policy = self._policies.get(mode)
        if policy is None:
            raise ValueError(f"No billing policy is configured for mode '{mode.value}'")
        return policy

    async def release_voice_session(self, tenant_id: str, session_id: str) -> None:
        # Ledgers must treat sessions they do not own as idempotent no-ops.
        for policy in self._policies.values():
            await policy.release_voice_session(tenant_id, session_id)

    async def settle_voice_session(
        self, tenant_id: str, session_id: str, billable_seconds: int
    ) -> None:
        # Ledgers must settle only reservations they own; all others are no-ops.
        for policy in self._policies.values():
            await policy.settle_voice_session(tenant_id, session_id, billable_seconds)


__all__ = ["BillingPolicyRegistry", "VoiceBillingPolicy"]
