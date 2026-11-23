"""Generic mutable per-session state helpers."""

from __future__ import annotations

import asyncio
from typing import Any

from chiefvoice.sessions import AcceptedSession


def collected_data_lock(session: AcceptedSession) -> asyncio.Lock:
    """Return the lock shared by per-call collectors and completion checks."""
    lock = session.metadata.get("_collected_data_lock")
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        session.metadata["_collected_data_lock"] = lock
    return lock


def initialize_collected_data(session: AcceptedSession) -> dict[str, Any]:
    """Ensure the session has a mutable dictionary for captured call values."""
    collected = session.metadata.get("collected_data")
    if not isinstance(collected, dict):
        collected = {}
        session.metadata["collected_data"] = collected
    return collected
