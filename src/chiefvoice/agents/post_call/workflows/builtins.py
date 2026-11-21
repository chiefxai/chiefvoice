"""Explicit registration for bundled workflow integrations."""

from typing import Any

from chiefvoice.agents.post_call.workflows import PostCallWorkflowRegistry


def build_builtin_workflow_registry(chat_model: Any) -> PostCallWorkflowRegistry:
    """Register bundled integrations at the provider composition boundary."""
    from chiefvoice.agents.post_call.workflows.attendance import AttendanceWorkflow

    registry = PostCallWorkflowRegistry()
    registry.register(AttendanceWorkflow(chat_model))
    return registry
