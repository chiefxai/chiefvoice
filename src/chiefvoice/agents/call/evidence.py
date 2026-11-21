"""Generic per-call evidence capture helpers."""

from __future__ import annotations

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
            "arguments": dict(arguments),
            "caller_transcript_at_execution": session.metadata.get("post_call_transcript", ""),
        }
    )
