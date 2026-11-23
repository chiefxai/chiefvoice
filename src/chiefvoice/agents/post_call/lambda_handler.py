"""AWS Lambda entrypoint for durable post-call jobs.

Configure the Lambda handler as `chiefvoice.agents.post_call.lambda_handler.handler`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from functools import lru_cache
import logging
import os

from chiefvoice.agents.post_call.base import PostCallAgent
from chiefvoice.agents.post_call.providers.vertex import create_vertex_post_call_agent
from chiefvoice.agents.post_call.providers.api_key_vertex import ApiKeyVertexPostCallAgent
from chiefvoice.agents.post_call.queue import SqlPostCallQueue
from chiefvoice.calls.store import SqlCallSessionStore
from chiefvoice.agents.template_store import SqlAgentTemplateDirectory
from chiefvoice.identity.api_keys import SqlApiKeyDirectory
from chiefvoice.cloud_projects.post_call import TenantVertexPostCallAgent
from chiefvoice.cloud_projects.store import SqlTenantVertexProjectDirectory
from chiefvoice.core.config import settings
from chiefvoice.observability import configure_logging
from chiefvoice.recordings.storage import create_recording_store

configure_logging()
logger = logging.getLogger(__name__)


@lru_cache(maxsize=128)
def _read_secret(secret_arn: str) -> str:
    """Read deployment secrets at invocation time; never put secret contents in Lambda env."""
    import boto3  # type: ignore[import-untyped]

    response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)
    value = response.get("SecretString")
    if not isinstance(value, str) or not value:
        raise RuntimeError("A configured post-call secret is empty or binary")
    return value


async def _process(event: Mapping[str, object]) -> dict[str, object]:
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
    has_global_vertex = bool(
        settings.vertex_project_isolation_enabled
        or (settings.vertex_project_id and settings.vertex_location)
    )
    if not has_global_vertex and not settings.credentials_encryption_key:
        raise RuntimeError("Per-API GCP credentials require CREDENTIALS_ENCRYPTION_KEY")

    queue: SqlPostCallQueue | None = None
    call_session_store: SqlCallSessionStore | None = None
    template_directory: SqlAgentTemplateDirectory | None = None
    directory: SqlTenantVertexProjectDirectory | None = None
    api_key_directory: SqlApiKeyDirectory | None = None
    try:
        queue = SqlPostCallQueue(
            database_url,
            post_call_key,
            recording_store=create_recording_store(),
        )
        call_session_store = (
            SqlCallSessionStore(database_url, settings.credentials_encryption_key)
            if settings.credentials_encryption_key
            else None
        )
        template_directory = (
            SqlAgentTemplateDirectory(database_url)
            if settings.credentials_encryption_key
            else None
        )
        if call_session_store is not None:
            queue.workflow_config_resolver = lambda tenant_id, session_id: (
                call_session_store.get_post_call_workflow_config(
                    tenant_id, session_id, template_directory
                )
            )
        await queue.initialize()

        agent: PostCallAgent | None = None
        if has_global_vertex and settings.vertex_project_isolation_enabled:
            directory = SqlTenantVertexProjectDirectory(database_url)
            await directory.initialize()
            agent = TenantVertexPostCallAgent(
                directory,
                location=settings.post_call_location,
                model=settings.post_call_model,
                credentials_json=credentials_json,
                credentials_path=settings.vertex_credentials_path,
            )
        elif has_global_vertex and settings.vertex_project_id:
            project_id = settings.vertex_project_id
            agent = create_vertex_post_call_agent(
                project_id=project_id,
                location=settings.post_call_location,
                model=settings.post_call_model,
                credentials_json=credentials_json,
                credentials_path=settings.vertex_credentials_path,
            )
        if settings.credentials_encryption_key:
            api_key_directory = SqlApiKeyDirectory(
                database_url, settings.credentials_encryption_key
            )
            await api_key_directory.initialize()
            agent = ApiKeyVertexPostCallAgent(
                api_key_directory, model=settings.post_call_model, fallback=agent
            )
        if agent is None:
            raise RuntimeError("No Vertex post-call agent configuration is available")
        processed = await queue.process_job(job_id, agent)
        return {"job_id": job_id, "status": "processed" if processed else "already_claimed_or_finished"}
    finally:
        resources = (
            ("tenant_project_directory", directory),
            ("api_key_directory", api_key_directory),
            ("call_session_store", call_session_store),
            ("template_directory", template_directory),
            ("post_call_queue", queue),
        )
        closed_resource_ids: set[int] = set()
        for resource_name, resource in resources:
            if resource is None or id(resource) in closed_resource_ids:
                continue
            closed_resource_ids.add(id(resource))
            try:
                await resource.close()
            except Exception as exc:
                # Never replace the initialization or processing failure with a
                # cleanup error, and keep provider details out of logs.
                logger.warning(
                    "post_call_resource_cleanup_failed",
                    extra={"resource": resource_name, "error_type": type(exc).__name__},
                )


def handler(event: Mapping[str, object], context: object) -> dict[str, object]:
    """Process a direct event or SQS batch; SQS retries failed records and DLQs poison jobs."""
    del context
    records = event.get("Records")
    if isinstance(records, list):
        failed: list[dict[str, str]] = []
        for record in records:
            if not isinstance(record, Mapping):
                continue
            message_id = record.get("messageId")
            body = record.get("body")
            try:
                if not isinstance(body, str):
                    raise ValueError("Malformed SQS body")
                payload = __import__("json").loads(body)
                asyncio.run(_process(payload))
            except Exception:
                if isinstance(message_id, str):
                    failed.append({"itemIdentifier": message_id})
        return {"batchItemFailures": failed}
    return asyncio.run(_process(event))
