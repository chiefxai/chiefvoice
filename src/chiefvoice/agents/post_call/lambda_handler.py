"""AWS Lambda entrypoint for durable post-call jobs.

Configure the Lambda handler as `chiefvoice.agents.post_call.lambda_handler.handler`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from chiefvoice.agents.post_call.providers.vertex import create_vertex_post_call_agent
from chiefvoice.agents.post_call.queue import SqlPostCallQueue
from chiefvoice.cloud_projects.post_call import TenantVertexPostCallAgent
from chiefvoice.cloud_projects.store import SqlTenantVertexProjectDirectory
from chiefvoice.core.config import settings


async def _process(event: Mapping[str, object]) -> dict[str, str]:
    job_id = event.get("job_id")
    if event.get("schema_version") != 1 or not isinstance(job_id, str) or not job_id:
        raise ValueError("Unsupported or malformed post-call job event")
    if not settings.database_url or not settings.post_call_encryption_key:
        raise RuntimeError("DATABASE_URL and POST_CALL_ENCRYPTION_KEY are required")
    if settings.post_call_agent_provider != "langchain_vertex":
        raise RuntimeError("Lambda handler currently requires POST_CALL_AGENT_PROVIDER=langchain_vertex")
    if not settings.vertex_location:
        raise RuntimeError("VERTEX_LOCATION is required")
    if not settings.vertex_project_isolation_enabled and not settings.vertex_project_id:
        raise RuntimeError("VERTEX_PROJECT_ID is required when project isolation is disabled")

    queue = SqlPostCallQueue(settings.database_url, settings.post_call_encryption_key)
    await queue.initialize()
    directory = None
    try:
        if settings.vertex_project_isolation_enabled:
            directory = SqlTenantVertexProjectDirectory(settings.database_url)
            await directory.initialize()
            agent = TenantVertexPostCallAgent(
                directory,
                location=settings.vertex_location,
                model=settings.post_call_model,
                credentials_json=settings.vertex_credentials_json,
                credentials_path=settings.vertex_credentials_path,
            )
        else:
            agent = create_vertex_post_call_agent(
                project_id=settings.vertex_project_id,
                location=settings.vertex_location,
                model=settings.post_call_model,
                credentials_json=settings.vertex_credentials_json,
                credentials_path=settings.vertex_credentials_path,
            )
        processed = await queue.process_job(job_id, agent)
        return {"job_id": job_id, "status": "processed" if processed else "already_claimed_or_finished"}
    finally:
        if directory is not None:
            await directory.close()
        await queue.close()


def handler(event: Mapping[str, object], context: object) -> dict[str, str]:
    """Synchronous AWS Lambda runtime entrypoint; a duplicate event is harmless."""
    del context
    return asyncio.run(_process(event))
