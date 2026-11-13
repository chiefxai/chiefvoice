from collections.abc import Sequence

from chiefvoice.agents.call.providers.base import RealtimeAgentProvider


class RealtimeAgentRegistry:
    """Maps configured realtime provider names to their implementations."""

    def __init__(self, providers: Sequence[RealtimeAgentProvider]):
        self._providers = {provider.name: provider for provider in providers}
        if len(self._providers) != len(providers):
            raise ValueError("Realtime agent provider names must be unique")

    def get(self, name: str) -> RealtimeAgentProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ValueError(f"Realtime agent provider is not configured: {name}") from exc


__all__ = ["RealtimeAgentProvider", "RealtimeAgentRegistry"]
