"""Conservative recovery for outbound requests with uncertain provider outcomes."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Protocol


logger = logging.getLogger(__name__)


class OutboundStartRecoveryStore(Protocol):
    async def flag_stale_outbound_starts(self, older_than: timedelta, limit: int = 100) -> int: ...


async def reconcile_stale_outbound_once(
    store: OutboundStartRecoveryStore,
    *,
    older_than_seconds: int,
    batch_size: int = 100,
) -> int:
    if older_than_seconds < 1:
        raise ValueError("The stale-start age must be positive")
    count = await store.flag_stale_outbound_starts(
        timedelta(seconds=older_than_seconds), limit=batch_size
    )
    if count:
        logger.error(
            "outbound_calls_require_provider_reconciliation",
            extra={"call_count": count, "older_than_seconds": older_than_seconds},
        )
    return count


async def run_outbound_start_reconciler(
    store: OutboundStartRecoveryStore,
    *,
    older_than_seconds: int,
    interval_seconds: int,
) -> None:
    if interval_seconds < 1:
        raise ValueError("The reconciliation interval must be positive")
    while True:
        try:
            await reconcile_stale_outbound_once(
                store, older_than_seconds=older_than_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "outbound_start_reconciliation_failed",
                extra={"error_type": type(exc).__name__},
            )
        await asyncio.sleep(interval_seconds)
