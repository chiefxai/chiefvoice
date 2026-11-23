"""Generic contracts for optional post-call workflow integrations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from chiefvoice.agents.post_call.base import CompletedCall, PostCallResult

WORKFLOW_RESULT_VERSION = 1
MAX_WORKFLOW_RESULT_BYTES = 32 * 1024


class PostCallWorkflow(Protocol):
    """A separately registered integration that interprets extracted call evidence."""

    workflow_id: str

    async def process(self, call: CompletedCall, result: PostCallResult) -> dict[str, Any]:
        """Return workflow-owned result data; core wraps it in the versioned extension."""


class PostCallWorkflowRegistry:
    """Explicit workflow registration; no workflow is selected from prompt text."""

    def __init__(self) -> None:
        self._workflows: dict[str, PostCallWorkflow] = {}

    def register(self, workflow: PostCallWorkflow) -> None:
        workflow_id = getattr(workflow, "workflow_id", None)
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            raise ValueError("A workflow must declare a non-empty workflow_id")
        if workflow_id in self._workflows:
            raise ValueError(f"Workflow is already registered: {workflow_id}")
        self._workflows[workflow_id] = workflow

    def get(self, workflow_id: str) -> PostCallWorkflow | None:
        return self._workflows.get(workflow_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._workflows))


def workflow_result_envelope(
    workflow_id: str, *, status: str, data: Mapping[str, Any] | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build the additive, versioned workflow extension shared by API responses."""
    envelope: dict[str, Any] = {
        "version": WORKFLOW_RESULT_VERSION,
        "workflow_id": workflow_id,
        "status": status,
    }
    if data is not None:
        envelope["data"] = dict(data)
    if error_code is not None:
        envelope["error_code"] = error_code
    return envelope
