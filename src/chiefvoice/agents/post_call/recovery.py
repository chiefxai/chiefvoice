from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.agents.post_call.fallback_spool import PostCallFallbackSpool
from chiefvoice.agents.post_call.queue import PostCallDispatcher
from chiefvoice.observability.setup import post_call_enqueue_fallback_event

logger = logging.getLogger(__name__)


class PostCallSessionStore(Protocol):
    async def pending_post_call(self, limit: int = 100) -> list[CompletedCall]: ...
    async def mark_post_call_enqueued(self, tenant_id: str, session_id: str) -> None: ...


async def run_call_session_reconciler(
    call_sessions: PostCallSessionStore,
    queue: PostCallDispatcher,
    *,
    poll_interval: float = 30.0,
    batch_size: int = 100,
) -> None:
    """Recover completed calls whose durable post-call job is missing or invalid."""
    while True:
        try:
            while True:
                calls = await call_sessions.pending_post_call(limit=batch_size)
                if not calls:
                    break
                for call in calls:
                    inserted = await queue.enqueue(call)
                    await call_sessions.mark_post_call_enqueued(
                        call.tenant_id, call.session_id
                    )
                    logger.info(
                        "post_call_session_reconciled",
                        extra={
                            "session_id": call.session_id,
                            "tenant_id": call.tenant_id,
                            "queue_record_created": inserted is not False,
                            "outcome": "enqueued" if inserted is not False else "already_present",
                        },
                    )
                if len(calls) < batch_size:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "post_call_session_reconciliation_failed",
                extra={"error_type": type(exc).__name__},
            )
        await asyncio.sleep(poll_interval)


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
