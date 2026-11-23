"""Generic per-call evidence capture helpers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from chiefvoice.sessions import AcceptedSession


def capture_tool_execution(
    session: AcceptedSession, *, tool_name: str, arguments: dict[str, Any]
) -> None:
    """Capture the executed tool claim with the caller transcript visible at that point."""
    events = session.metadata.setdefault("tool_events", [])
    if not isinstance(events, list):
        return
    events.append(
        {
            "tool_name": tool_name,
            # Preserve the event as it was observed even if a provider reuses
            # and later mutates nested tool argument objects.
            "arguments": deepcopy(arguments),
            "caller_transcript_at_execution": session.metadata.get("post_call_transcript", ""),
        }
    )
