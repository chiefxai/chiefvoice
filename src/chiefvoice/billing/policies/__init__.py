from collections.abc import Sequence
from datetime import datetime

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

    async def settle_voice_session_at(
        self, tenant_id: str, session_id: str, billable_seconds: int, ended_at: datetime
    ) -> bool:
        limit_event = False
        for policy in self._policies.values():
            method = getattr(policy, "settle_voice_session_at", None)
            if method is None:
                await policy.settle_voice_session(tenant_id, session_id, billable_seconds)
            else:
                limit_event = (
                    await method(tenant_id, session_id, billable_seconds, ended_at)
                ) or limit_event
        return limit_event

    async def mark_voice_session_answered(
        self, tenant_id: str, session_id: str, answered_at: datetime
    ) -> datetime | None:
        canonical_answered_at = None
        for policy in self._policies.values():
            method = getattr(policy, "mark_voice_session_answered", None)
            if method is not None:
                result = await method(tenant_id, session_id, answered_at)
                if result is not None:
                    canonical_answered_at = result
        return canonical_answered_at

    async def mark_duration_limit_event(self, tenant_id: str, session_id: str) -> bool:
        emitted = False
        for policy in self._policies.values():
            method = getattr(policy, "mark_duration_limit_event", None)
            if method is not None:
                emitted = (await method(tenant_id, session_id)) or emitted
        return emitted


__all__ = ["BillingPolicyRegistry", "VoiceBillingPolicy"]
