from __future__ import annotations

import asyncio
import logging

from chiefvoice.agents.post_call.fallback_spool import PostCallFallbackSpool
from chiefvoice.agents.post_call.queue import PostCallDispatcher
from chiefvoice.observability.setup import post_call_enqueue_fallback_event

logger = logging.getLogger(__name__)


async def run_spool_reconciler(
    spool: PostCallFallbackSpool,
    queue: PostCallDispatcher,
    *,
    poll_interval: float = 30.0,
    batch_size: int = 100,
) -> None:
    """Move S3-staged calls into the Postgres outbox, deleting only after confirmation."""
    while True:
        try:
            staged_calls = await spool.list_pending(batch_size)
            for call in staged_calls:
                try:
                    # False means an idempotent row for this session already exists.
                    await queue.enqueue(call)
                    await spool.delete(call.session_id)
                    logger.info(
                        "post_call_fallback_reconciled",
                        extra={"session_id": call.session_id, "tenant_id": call.tenant_id},
                    )
                    post_call_enqueue_fallback_event("reconciled")
                except Exception as exc:
                    post_call_enqueue_fallback_event("reconcile_failed")
                    logger.error(
                        "post_call_fallback_reconcile_failed",
                        extra={"session_id": call.session_id, "error_type": type(exc).__name__},
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            post_call_enqueue_fallback_event("scan_failed")
            logger.error(
                "post_call_fallback_scan_failed",
                extra={"error_type": type(exc).__name__},
            )
        await asyncio.sleep(poll_interval)
