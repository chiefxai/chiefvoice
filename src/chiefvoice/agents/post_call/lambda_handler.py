"""AWS Lambda entrypoint for durable post-call jobs.

Configure the Lambda handler as `chiefvoice.agents.post_call.lambda_handler.handler`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from functools import lru_cache
import os

from chiefvoice.agents.post_call.base import PostCallAgent
from chiefvoice.agents.post_call.providers.vertex import create_vertex_post_call_agent
from chiefvoice.agents.post_call.queue import SqlPostCallQueue
from chiefvoice.cloud_projects.post_call import TenantVertexPostCallAgent
from chiefvoice.cloud_projects.store import SqlTenantVertexProjectDirectory
from chiefvoice.core.config import settings
from chiefvoice.observability import configure_logging

configure_logging()


@lru_cache(maxsize=128)
def _read_secret(secret_arn: str) -> str:
    """Read deployment secrets at invocation time; never put secret contents in Lambda env."""
    import boto3  # type: ignore[import-untyped]

    response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
    value = response.get("SecretString")
    if not isinstance(value, str) or not value:
        raise RuntimeError("A configured post-call secret is empty or binary")
    return value


async def _process(event: Mapping[str, object]) -> dict[str, str]:
    job_id = event.get("job_id")
    if event.get("schema_version") != 1 or not isinstance(job_id, str) or not job_id:
        raise ValueError("Unsupported or malformed post-call job event")
    database_url = settings.database_url
    database_secret_arn = os.getenv("DATABASE_URL_SECRET_ARN")
    if database_secret_arn:
        database_url = _read_secret(database_secret_arn)
    post_call_key = settings.post_call_encryption_key
    key_secret_arn = os.getenv("POST_CALL_ENCRYPTION_KEY_SECRET_ARN")
    if key_secret_arn:
        post_call_key = _read_secret(key_secret_arn)
    credentials_json = settings.vertex_credentials_json
    credentials_secret_arn = os.getenv("GOOGLE_CREDENTIALS_SECRET_ARN")
    if credentials_secret_arn:
        credentials_json = _read_secret(credentials_secret_arn)
    if not database_url or not post_call_key:
        raise RuntimeError("DATABASE_URL and POST_CALL_ENCRYPTION_KEY are required")
    if settings.post_call_agent_provider != "langchain_vertex":
        raise RuntimeError("Lambda handler currently requires POST_CALL_AGENT_PROVIDER=langchain_vertex")
    if not settings.vertex_location:
        raise RuntimeError("VERTEX_LOCATION is required")
    if not settings.vertex_project_isolation_enabled and not settings.vertex_project_id:
        raise RuntimeError("VERTEX_PROJECT_ID is required when project isolation is disabled")

    queue = SqlPostCallQueue(database_url, post_call_key)
    await queue.initialize()
    directory: SqlTenantVertexProjectDirectory | None = None
    agent: PostCallAgent
    try:
        if settings.vertex_project_isolation_enabled:
            directory = SqlTenantVertexProjectDirectory(database_url)
            await directory.initialize()
            agent = TenantVertexPostCallAgent(
                directory,
                location=settings.vertex_location,
                model=settings.post_call_model,
                credentials_json=credentials_json,
                credentials_path=settings.vertex_credentials_path,
            )
        else:
            project_id = settings.vertex_project_id
            if not project_id:
                raise RuntimeError("VERTEX_PROJECT_ID is required when project isolation is disabled")
            agent = create_vertex_post_call_agent(
                project_id=project_id,
                location=settings.vertex_location,
                model=settings.post_call_model,
                credentials_json=credentials_json,
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
