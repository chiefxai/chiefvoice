import logging
import asyncio
import json
from datetime import timedelta
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from contextlib import suppress
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pipecat.pipeline.pipeline import Pipeline
from pipecat.transports.base_transport import BaseTransport

from chiefvoice.identity.auth import OidcTokenVerifier
from chiefvoice.identity.api_key_routes import router as api_key_router
from chiefvoice.identity.api_keys import SqlApiKeyDirectory
from chiefvoice.accounts.routes import router as accounts_router
from chiefvoice.accounts.admin_routes import router as admin_customers_router
from chiefvoice.accounts.store import SqlAccountStore
from chiefvoice.agents.call.configs.routes import router as agent_profiles_router
from chiefvoice.agents.call.configs.profiles import AgentProfileDirectory
from chiefvoice.agents.configuration_store import SqlAgentConfigurationDirectory
from chiefvoice.agents.template_store import SqlAgentTemplateDirectory
from chiefvoice.agents.template_routes import router as agent_template_router
from chiefvoice.agents.call.configs.store import SqlAgentProfileDirectory
from chiefvoice.agents.call.factory import create_realtime_pipeline_factory
from chiefvoice.agents.call.prepared_session import PreparedRealtimeSessionDirectory
from chiefvoice.agents.call.providers import RealtimeAgentRegistry
from chiefvoice.agents.call.providers.vertex import VertexGeminiLiveProvider
from chiefvoice.agents.post_call.providers.vertex import create_vertex_post_call_agent
from chiefvoice.agents.post_call.providers.api_key_vertex import ApiKeyVertexPostCallAgent
from chiefvoice.agents.post_call.lambda_dispatch import LambdaAsyncInvoker
from chiefvoice.agents.post_call.gcp_function_dispatch import GoogleCloudFunctionInvoker
from chiefvoice.agents.post_call.sqs_dispatch import SqsPostCallInvoker
from chiefvoice.agents.post_call.cloud_tasks_dispatch import CloudTasksPostCallInvoker
from chiefvoice.agents.post_call.fallback_spool import S3PostCallFallbackSpool
from chiefvoice.agents.post_call.recovery import (
    run_call_session_reconciler,
    run_spool_reconciler,
)
from chiefvoice.agents.post_call.replay import SqlPostCallReplayService
from chiefvoice.agents.post_call.queue import PostCallDispatcher, SqlPostCallQueue
from chiefvoice.agents.post_call.queue import SqlPostCallWebhookDirectory
from chiefvoice.agents.post_call.webhook_routes import router as post_call_webhook_router
from chiefvoice.calls.coordinator import CallCoordinator
from chiefvoice.calls.recovery import run_outbound_start_reconciler
from chiefvoice.calls.store import SqlCallSessionStore
from chiefvoice.channels.base import ChannelAdapter
from chiefvoice.core.config import settings
from chiefvoice.cloud_projects.google_provisioner import GoogleCloudTenantProjectProvisioner
from chiefvoice.cloud_projects.post_call import TenantVertexPostCallAgent
from chiefvoice.cloud_projects.store import SqlTenantVertexProjectDirectory
from chiefvoice.observability import configure_logging, instrument_app
from chiefvoice.billing import SubscriptionAuthorizer
from chiefvoice.billing import BillingPolicyRegistry, PostpaidBillingPolicy, PrepaidBillingPolicy
from chiefvoice.billing.routes import router as billing_router
from chiefvoice.billing.store import SqlBillingDirectory
from chiefvoice.channels.vobiz.adapter import VobizAdapter
from chiefvoice.channels.vobiz.replay import SqlVobizCallbackNonceStore
from chiefvoice.channels.vobiz.routes import router as vobiz_router
from chiefvoice.channels.integration_routes import router as integrations_router
from chiefvoice.channels.integration_store import SqlProviderIntegrationStore
from chiefvoice.recordings.storage import create_recording_store
from chiefvoice.data_lifecycle.retention import (
    RetentionRunner,
    StaticRetentionPolicyDirectory,
    run_retention_periodically,
)
from chiefvoice.data_lifecycle.backup import (
    BackupRunner,
    PostgresBackupRunLock,
    StaticBackupPolicyDirectory,
    run_backups_periodically,
)
from chiefvoice.data_lifecycle.postgres_backup import EncryptedPostgresBackupProvider
from chiefvoice.storage.s3 import create_s3_compatible_client
from chiefvoice.sessions import AcceptedSession
from chiefvoice.calls.routes import router as calls_router

configure_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    integration_store = getattr(_app.state, "provider_integration_store", None)
    account_store = getattr(_app.state, "account_store", None)
    api_key_directory = getattr(_app.state, "api_key_directory", None)
    agent_configuration_directory = getattr(_app.state, "agent_configuration_directory", None)
    agent_template_directory = getattr(_app.state, "agent_template_directory", None)
    post_call_webhook_directory = getattr(_app.state, "post_call_webhook_directory", None)
    agent_profile_directory = getattr(_app.state, "agent_profile_directory", None)
    post_call_dispatcher = getattr(_app.state, "post_call_dispatcher", None)
    post_call_agent = getattr(_app.state, "post_call_agent", None)
    post_call_async_invoker = getattr(_app.state, "post_call_async_invoker", None)
    post_call_replay_service = getattr(_app.state, "post_call_replay_service", None)
    post_call_recovery_spool = getattr(_app.state, "post_call_recovery_spool", None)
    call_session_store = getattr(_app.state, "call_session_store", None)
    vobiz_callback_nonce_store = getattr(_app.state, "vobiz_callback_nonce_store", None)
    billing_directory = getattr(_app.state, "billing_directory", None)
    recording_store = getattr(_app.state, "recording_store", None)
    backup_provider = getattr(_app.state, "backup_provider", None)
    backup_execution_lock = getattr(_app.state, "backup_execution_lock", None)
    vertex_project_directory = getattr(_app.state, "vertex_project_directory", None)
    vertex_project_provisioner = getattr(_app.state, "vertex_project_provisioner", None)
    worker_tasks: list[asyncio.Task] = []
    storage_checks = getattr(_app.state, "object_storage_checks", [])
    _app.state.object_storage_ready = not storage_checks
    try:
        if account_store is not None:
            await account_store.initialize()
        if call_session_store is not None:
            await call_session_store.initialize()
            worker_tasks.append(asyncio.create_task(
                run_outbound_start_reconciler(
                    call_session_store,
                    older_than_seconds=settings.outbound_start_reconciliation_after_seconds,
                    interval_seconds=settings.outbound_start_reconciliation_interval_seconds,
                )
            ))
        if vobiz_callback_nonce_store is not None:
            await vobiz_callback_nonce_store.initialize()
        if billing_directory is not None:
            await billing_directory.initialize()
        if integration_store is not None:
            await integration_store.initialize()
        if api_key_directory is not None:
            await api_key_directory.initialize()
        if agent_configuration_directory is not None:
            await agent_configuration_directory.initialize()
        if agent_template_directory is not None:
            await agent_template_directory.initialize()
        if post_call_webhook_directory is not None:
            await post_call_webhook_directory.initialize()
        if agent_profile_directory is not None:
            await agent_profile_directory.initialize()
        if vertex_project_directory is not None:
            await vertex_project_directory.initialize()
            worker_tasks.append(asyncio.create_task(
                vertex_project_directory.run_worker(vertex_project_provisioner)
            ))
        if isinstance(post_call_dispatcher, SqlPostCallQueue):
            await post_call_dispatcher.initialize()
            if call_session_store is not None and post_call_agent is not None:
                # The transcript checkpoint is written throughout the call, so a VM
                # restart after hangup can still enqueue analysis exactly once.
                while True:
                    pending_calls = await call_session_store.pending_post_call(limit=100)
                    if not pending_calls:
                        break
                    for completed_call in pending_calls:
                        await post_call_dispatcher.enqueue(completed_call)
                        await call_session_store.mark_post_call_enqueued(
                            completed_call.tenant_id, completed_call.session_id
                        )
                    if len(pending_calls) < 100:
                        break
                worker_tasks.append(asyncio.create_task(
                    run_call_session_reconciler(call_session_store, post_call_dispatcher)
                ))
        if post_call_replay_service is not None:
            await post_call_replay_service.initialize()
        for check_storage in storage_checks:
            await check_storage()
        _app.state.object_storage_ready = True
        if isinstance(post_call_dispatcher, SqlPostCallQueue):
            if post_call_async_invoker is not None:
                worker_tasks.append(asyncio.create_task(post_call_dispatcher.run_async_dispatcher(
                    post_call_async_invoker,
                    redispatch_after=timedelta(seconds=settings.post_call_redispatch_after_seconds),
                )))
            elif post_call_agent is not None:
                worker_tasks.append(asyncio.create_task(post_call_dispatcher.run_worker(post_call_agent)))
            else:
                worker_tasks.append(asyncio.create_task(post_call_dispatcher.run_delivery_worker()))
        if post_call_dispatcher is not None and post_call_recovery_spool is not None:
            worker_tasks.append(asyncio.create_task(
                run_spool_reconciler(post_call_recovery_spool, post_call_dispatcher)
            ))
        retention_runner = getattr(_app.state, "retention_runner", None)
        if retention_runner is not None:
            worker_tasks.append(asyncio.create_task(
                run_retention_periodically(retention_runner, settings.retention_interval_seconds)
            ))
        backup_runner = getattr(_app.state, "backup_runner", None)
        if backup_runner is not None:
            worker_tasks.append(asyncio.create_task(
                run_backups_periodically(
                    backup_runner,
                    settings.backup_interval_seconds,
                    backup_execution_lock,
                )
            ))
        logger.info("application_started", extra={"event": "application_started"})
        yield
    finally:
        prepared_realtime_sessions = getattr(
            _app.state, "prepared_realtime_sessions", None
        )
        if prepared_realtime_sessions is not None:
            await prepared_realtime_sessions.close_all()
        for worker_task in worker_tasks:
            worker_task.cancel()
        for worker_task in worker_tasks:
            with suppress(asyncio.CancelledError):
                await worker_task
        if isinstance(post_call_dispatcher, SqlPostCallQueue):
            await post_call_dispatcher.close()
        if post_call_replay_service is not None:
            await post_call_replay_service.close()
        if post_call_recovery_spool is not None:
            await post_call_recovery_spool.close()
        if billing_directory is not None:
            await billing_directory.close()
        if backup_execution_lock is not None:
            await backup_execution_lock.close()
        for storage_component in (recording_store, backup_provider):
            close = getattr(storage_component, "close", None)
            if close is not None:
                await close()
        if integration_store is not None:
            await integration_store.close()
        if post_call_webhook_directory is not None:
            await post_call_webhook_directory.close()
        if agent_profile_directory is not None:
            await agent_profile_directory.close()
        if api_key_directory is not None:
            await api_key_directory.close()
        if agent_configuration_directory is not None:
            await agent_configuration_directory.close()
        if agent_template_directory is not None:
            await agent_template_directory.close()
        if account_store is not None:
            await account_store.close()
        if call_session_store is not None:
            await call_session_store.close()
        if vobiz_callback_nonce_store is not None:
            await vobiz_callback_nonce_store.close()
        if vertex_project_directory is not None:
            await vertex_project_directory.close()
        logger.info("application_stopped", extra={"event": "application_stopped"})


def create_app(
    *,
    channel_adapters: Mapping[str, ChannelAdapter] | None = None,
    subscription_authorizer: SubscriptionAuthorizer | None = None,
    token_verifier: OidcTokenVerifier | None = None,
    provider_integration_store: SqlProviderIntegrationStore | None = None,
    account_store: SqlAccountStore | None = None,
    api_key_directory: SqlApiKeyDirectory | None = None,
    agent_configuration_directory: SqlAgentConfigurationDirectory | None = None,
    agent_template_directory: SqlAgentTemplateDirectory | None = None,
    post_call_webhook_directory: SqlPostCallWebhookDirectory | None = None,
    agent_profile_directory: AgentProfileDirectory | None = None,
    billing_directory: SqlBillingDirectory | None = None,
    post_call_dispatcher: PostCallDispatcher | None = None,
    voice_pipeline_factory: Callable[
        [AcceptedSession, BaseTransport], Pipeline | Awaitable[Pipeline]
    ]
    | None = None,
    realtime_preflight: Callable[[AcceptedSession], Awaitable[None]] | None = None,
    realtime_preflight_cleanup: Callable[[str, str], Awaitable[None]] | None = None,
) -> FastAPI:
    application = FastAPI(title="Chiefvoice", version="0.1.0", lifespan=lifespan)
    post_call_agent = None
    prepared_realtime_sessions = PreparedRealtimeSessionDirectory()
    vertex_project_directory = None
    vertex_project_provisioner = None
    if api_key_directory is None and settings.database_url:
        api_key_directory = SqlApiKeyDirectory(
            settings.database_url, settings.credentials_encryption_key
        )
    if agent_configuration_directory is None and settings.database_url:
        agent_configuration_directory = SqlAgentConfigurationDirectory(
            settings.database_url
        )
    if agent_template_directory is None and settings.database_url:
        agent_template_directory = SqlAgentTemplateDirectory(settings.database_url)
    if settings.vertex_project_isolation_enabled:
        required_gcp_config = {
            "DATABASE_URL": settings.database_url,
            "GCP_PROJECT_PARENT": settings.gcp_project_parent,
            "GCP_BILLING_ACCOUNT_ID": settings.gcp_billing_account_id,
            "GCP_RUNTIME_SERVICE_ACCOUNT": settings.gcp_runtime_service_account,
            "GCP_BUDGET_API_PROJECT_ID": settings.gcp_budget_api_project_id,
            "GCP_VERTEX_MONTHLY_SPEND_CAP": settings.gcp_vertex_monthly_spend_cap,
            "VERTEX_LOCATION": settings.vertex_location,
        }
        missing = [key for key, value in required_gcp_config.items() if not value]
        if missing:
            raise ValueError("Vertex project isolation requires: " + ", ".join(missing))
        vertex_project_directory = SqlTenantVertexProjectDirectory(settings.database_url)
        additional_vertex_identities = ()
        if settings.post_call_execution_backend in {"gcp_function", "gcp_cloud_tasks"}:
            if not settings.gcp_post_call_service_account:
                raise ValueError(
                    "GCP_POST_CALL_SERVICE_ACCOUNT is required for the GCP post-call function backend"
                )
            additional_vertex_identities = (settings.gcp_post_call_service_account,)
        vertex_project_provisioner = GoogleCloudTenantProjectProvisioner(
            parent=settings.gcp_project_parent,
            billing_account_id=settings.gcp_billing_account_id,
            runtime_service_account=settings.gcp_runtime_service_account,
            additional_runtime_service_accounts=additional_vertex_identities,
            credentials_json=settings.vertex_credentials_json,
            credentials_path=settings.vertex_credentials_path,
            budget_api_project_id=settings.gcp_budget_api_project_id,
            monthly_vertex_spend_cap=settings.gcp_vertex_monthly_spend_cap,
        )
    if (
        settings.post_call_agent_provider == "langchain_vertex"
        and (settings.vertex_project_id or settings.vertex_project_isolation_enabled)
        and settings.post_call_location
    ):
        if settings.vertex_project_isolation_enabled:
            post_call_agent = TenantVertexPostCallAgent(
                vertex_project_directory,
                location=settings.post_call_location,
                model=settings.post_call_model,
                credentials_json=settings.vertex_credentials_json,
                credentials_path=settings.vertex_credentials_path,
            )
        else:
            post_call_agent = create_vertex_post_call_agent(
                project_id=settings.vertex_project_id,
                location=settings.post_call_location,
                model=settings.post_call_model,
                credentials_json=settings.vertex_credentials_json,
                credentials_path=settings.vertex_credentials_path,
            )
    if settings.post_call_agent_provider == "langchain_vertex" and api_key_directory is not None:
        post_call_agent = ApiKeyVertexPostCallAgent(
            api_key_directory, model=settings.post_call_model, fallback=post_call_agent
        )
    application.state.post_call_agent = post_call_agent
    if settings.post_call_execution_backend not in {"in_process", "lambda", "gcp_function", "sqs", "gcp_cloud_tasks"}:
        raise ValueError(
            "POST_CALL_EXECUTION_BACKEND must be in_process, lambda, gcp_function, sqs, or gcp_cloud_tasks"
        )
    post_call_async_invoker: Any = None
    if settings.post_call_execution_backend == "lambda":
        if not settings.post_call_lambda_function_name:
            raise ValueError("POST_CALL_LAMBDA_FUNCTION_NAME is required for Lambda execution")
        if (
            settings.post_call_agent_provider != "langchain_vertex"
            or (not settings.vertex_project_id and not settings.vertex_project_isolation_enabled)
            or not settings.vertex_location
            or not settings.database_url
            or not settings.post_call_encryption_key
        ):
            raise ValueError(
                "Lambda post-call execution requires LangChain Vertex, Vertex project isolation or "
                "a Vertex project/location, "
                "DATABASE_URL, and POST_CALL_ENCRYPTION_KEY"
            )
        post_call_async_invoker = LambdaAsyncInvoker(
            settings.post_call_lambda_function_name,
            settings.post_call_lambda_region,
        )
    elif settings.post_call_execution_backend == "gcp_function":
        if not settings.post_call_gcp_function_url:
            raise ValueError("POST_CALL_GCP_FUNCTION_URL is required for the GCP function backend")
        if (
            settings.post_call_agent_provider != "langchain_vertex"
            or (not settings.vertex_project_id and not settings.vertex_project_isolation_enabled)
            or not settings.vertex_location
            or not settings.database_url
            or not settings.post_call_encryption_key
        ):
            raise ValueError(
                "GCP function post-call execution requires LangChain Vertex, Vertex project "
                "isolation or project/location, DATABASE_URL, and POST_CALL_ENCRYPTION_KEY"
            )
        post_call_async_invoker = GoogleCloudFunctionInvoker(settings.post_call_gcp_function_url)
    elif settings.post_call_execution_backend == "sqs":
        if not settings.post_call_sqs_queue_url:
            raise ValueError("POST_CALL_SQS_QUEUE_URL is required for the SQS backend")
        post_call_async_invoker = SqsPostCallInvoker(settings.post_call_sqs_queue_url)
    elif settings.post_call_execution_backend == "gcp_cloud_tasks":
        if not settings.post_call_gcp_function_url or not settings.post_call_cloud_tasks_queue:
            raise ValueError("POST_CALL_GCP_FUNCTION_URL and POST_CALL_CLOUD_TASKS_QUEUE are required")
        if not settings.gcp_cloud_tasks_service_account:
            raise ValueError("GCP_CLOUD_TASKS_SERVICE_ACCOUNT is required")
        post_call_async_invoker = CloudTasksPostCallInvoker(
            settings.post_call_cloud_tasks_queue,
            settings.post_call_gcp_function_url,
            settings.gcp_cloud_tasks_service_account,
        )
    application.state.post_call_async_invoker = post_call_async_invoker
    application.state.vertex_project_directory = vertex_project_directory
    application.state.vertex_project_provisioner = vertex_project_provisioner
    recording_store = create_recording_store()
    post_call_required = settings.post_call_agent_provider != "disabled"
    if (
        post_call_dispatcher is None
        and settings.database_url
        and settings.post_call_encryption_key
    ):
        post_call_dispatcher = SqlPostCallQueue(
            settings.database_url,
            settings.post_call_encryption_key,
            poll_interval=settings.post_call_dispatch_interval_seconds,
            recording_store=recording_store,
        )
    application.state.post_call_dispatcher = post_call_dispatcher
    post_call_replay_service = (
        SqlPostCallReplayService(settings.database_url)
        if settings.database_url and settings.post_call_encryption_key
        else None
    )
    application.state.post_call_replay_service = post_call_replay_service
    application.state.post_call_required = post_call_required
    if agent_profile_directory is None and settings.database_url:
        agent_profile_directory = SqlAgentProfileDirectory(settings.database_url)
    object_storage_checks: list[Callable[[], Awaitable[None]]] = []
    if recording_store is not None and hasattr(recording_store, "check_storage"):
        object_storage_checks.append(recording_store.check_storage)
    post_call_recovery_spool = None
    if (
        post_call_required
        and post_call_dispatcher is not None
        and settings.database_url
        and settings.post_call_encryption_key
        and settings.object_storage_bucket
    ):
        post_call_recovery_spool = S3PostCallFallbackSpool(
            settings.object_storage_bucket,
            settings.post_call_encryption_key,
        )
        object_storage_checks.append(post_call_recovery_spool.check_storage)
    application.state.recording_store = recording_store
    application.state.post_call_recovery_spool = post_call_recovery_spool
    if voice_pipeline_factory is None and settings.realtime_provider == "vertex":
        if (settings.vertex_project_id or settings.vertex_project_isolation_enabled or settings.google_ai_api_key or api_key_directory):
            vertex_provider = VertexGeminiLiveProvider(
                project_id=settings.vertex_project_id,
                location=settings.vertex_location or "us-central1",
                model=settings.vertex_model,
                voice=settings.vertex_voice,
                system_instruction=settings.vertex_system_instruction,
                profile_directory=agent_profile_directory,
                credentials_json=settings.vertex_credentials_json,
                credentials_path=settings.vertex_credentials_path,
                project_directory=vertex_project_directory,
                require_tenant_project=settings.vertex_project_isolation_enabled,
                google_ai_api_key=settings.google_ai_api_key,
                google_ai_model=settings.google_ai_live_model,
                api_key_directory=api_key_directory,
                prepared_sessions=prepared_realtime_sessions,
            )
            voice_pipeline_factory = create_realtime_pipeline_factory(
                RealtimeAgentRegistry([vertex_provider]),
                "vertex",
                recording_store=recording_store,
                prepared_sessions=prepared_realtime_sessions,
            )
            if realtime_preflight is None:
                realtime_preflight = vertex_provider.preflight
            if realtime_preflight_cleanup is None:
                realtime_preflight_cleanup = vertex_provider.cleanup_prepared_session
    adapters = dict(channel_adapters or {})
    if account_store is None and settings.database_url:
        account_store = SqlAccountStore(settings.database_url)
    if billing_directory is None and settings.database_url:
        billing_directory = SqlBillingDirectory(settings.database_url)
    if subscription_authorizer is None and billing_directory is not None:
        subscription_authorizer = SubscriptionAuthorizer(
            billing_directory,
            BillingPolicyRegistry(
                [
                    PrepaidBillingPolicy(billing_directory),
                    PostpaidBillingPolicy(billing_directory),
                ]
            ),
        )
    if provider_integration_store is None and settings.database_url and settings.credentials_encryption_key:
        provider_integration_store = SqlProviderIntegrationStore(
            settings.database_url, settings.credentials_encryption_key
        )
    if (
        post_call_webhook_directory is None
        and settings.database_url
        and settings.post_call_encryption_key
    ):
        post_call_webhook_directory = SqlPostCallWebhookDirectory(
            settings.database_url, settings.post_call_encryption_key
        )
    call_session_store = (
        SqlCallSessionStore(settings.database_url, settings.credentials_encryption_key)
        if settings.database_url and settings.credentials_encryption_key
        else None
    )
    vobiz_callback_nonce_store = (
        SqlVobizCallbackNonceStore(settings.database_url)
        if settings.database_url
        else None
    )
    retention_policies = StaticRetentionPolicyDirectory.from_json(settings.retention_policies_json)
    retention_runner = None
    if retention_policies.policies:
        if settings.retention_interval_seconds < 60:
            raise ValueError("RETENTION_INTERVAL_SECONDS must be at least 60")
        targets = []
        if recording_store is not None:
            targets.append(recording_store)
        if isinstance(post_call_dispatcher, SqlPostCallQueue):
            targets.append(post_call_dispatcher)
        if call_session_store is not None:
            targets.append(call_session_store)
        supported = {target.data_class for target in targets}
        unsupported = {
            policy.data_class for policy in retention_policies.policies
            if policy.enabled and not policy.legal_hold and policy.data_class not in supported
        }
        if unsupported:
            names = ", ".join(sorted(item.value for item in unsupported))
            raise ValueError(f"Configured retention classes have no active storage target: {names}")
        retention_runner = RetentionRunner(retention_policies, targets)
    backup_policies = StaticBackupPolicyDirectory.from_json(settings.backup_policies_json)
    backup_runner = None
    backup_provider = None
    backup_execution_lock = None
    if backup_policies.policies:
        if settings.backup_interval_seconds < 60:
            raise ValueError("BACKUP_INTERVAL_SECONDS must be at least 60")
        if not settings.database_url:
            raise ValueError("DATABASE_URL is required when BACKUP_POLICIES_JSON is configured")
        if not settings.backup_encryption_key:
            raise ValueError("BACKUP_ENCRYPTION_KEY is required when backups are enabled")
        try:
            restore_targets = json.loads(settings.backup_restore_targets_json or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("BACKUP_RESTORE_TARGETS_JSON must be a JSON list of DSNs") from exc
        if not isinstance(restore_targets, list) or any(not isinstance(item, str) for item in restore_targets):
            raise ValueError("BACKUP_RESTORE_TARGETS_JSON must be a JSON list of DSNs")
        backup_backend = settings.backup_storage_backend
        if backup_backend == "s3":
            if not settings.object_storage_bucket:
                raise ValueError("OBJECT_STORAGE_BUCKET is required for S3 database backups")
            provider = EncryptedPostgresBackupProvider(
                settings.database_url,
                settings.backup_directory,
                settings.backup_encryption_key,
                allowed_restore_targets=tuple(restore_targets),
                s3_client=create_s3_compatible_client(),
                s3_bucket=settings.object_storage_bucket,
                s3_prefix="backups",
            )
            object_storage_checks.append(provider.check_storage)
        elif backup_backend == "local":
            if settings.app_env.lower() != "local":
                raise ValueError("Local backup storage is allowed only in local development")
            provider = EncryptedPostgresBackupProvider(
                settings.database_url,
                settings.backup_directory,
                settings.backup_encryption_key,
                allowed_restore_targets=tuple(restore_targets),
            )
        else:
            raise ValueError("BACKUP_STORAGE_BACKEND must be 'local' or 's3'")
        backup_provider = provider
        backup_runner = BackupRunner(backup_policies, provider)
        backup_execution_lock = PostgresBackupRunLock(settings.database_url)
    vobiz_adapter = adapters.get("vobiz")
    if vobiz_adapter is None and settings.vobiz_webhook_secret and provider_integration_store is not None:
        vobiz_adapter = VobizAdapter(
            provider_integration_store,
            vobiz_callback_nonce_store,
            call_session_store,
        )
        adapters["vobiz"] = vobiz_adapter
    application.state.provider_integration_store = provider_integration_store
    application.state.account_store = account_store
    application.state.call_session_store = call_session_store
    application.state.vobiz_callback_nonce_store = vobiz_callback_nonce_store
    application.state.billing_directory = billing_directory
    application.state.api_key_directory = api_key_directory
    application.state.agent_configuration_directory = agent_configuration_directory
    application.state.agent_template_directory = agent_template_directory
    application.state.post_call_webhook_directory = post_call_webhook_directory
    application.state.agent_profile_directory = agent_profile_directory
    application.state.object_storage_checks = object_storage_checks
    application.state.object_storage_ready = not object_storage_checks
    application.state.retention_runner = retention_runner
    application.state.backup_runner = backup_runner
    application.state.backup_provider = backup_provider
    application.state.backup_execution_lock = backup_execution_lock
    application.state.channel_adapters = adapters
    application.state.vobiz_adapter = vobiz_adapter
    application.state.subscription_authorizer = subscription_authorizer
    application.state.call_coordinator = (
        CallCoordinator(
            application.state.channel_adapters,
            subscription_authorizer,
            post_call_dispatcher=post_call_dispatcher,
            post_call_required=post_call_required,
            post_call_available=(post_call_agent is not None and post_call_dispatcher is not None),
            agent_profile_directory=agent_profile_directory,
            post_call_fallback_spool=post_call_recovery_spool,
            vertex_project_directory=vertex_project_directory,
            require_tenant_vertex_project=settings.vertex_project_isolation_enabled,
            call_session_store=call_session_store,
            api_key_directory=api_key_directory,
            realtime_preflight=realtime_preflight,
            realtime_preflight_cleanup=realtime_preflight_cleanup,
        )
        if subscription_authorizer is not None
        else None
    )
    application.state.voice_pipeline_factory = voice_pipeline_factory
    application.state.prepared_realtime_sessions = prepared_realtime_sessions
    if token_verifier is None and settings.oidc_issuer and settings.oidc_audience and settings.oidc_jwks_url:
        token_verifier = OidcTokenVerifier(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_url=settings.oidc_jwks_url,
            tenant_claim=settings.oidc_tenant_claim,
            algorithms=settings.oidc_algorithms,
        )
    application.state.token_verifier = token_verifier
    instrument_app(application)
    application.include_router(calls_router)
    application.include_router(accounts_router)
    application.include_router(admin_customers_router)
    application.include_router(vobiz_router)
    application.include_router(integrations_router)
    application.include_router(api_key_router)
    application.include_router(post_call_webhook_router)
    application.include_router(agent_profiles_router)
    application.include_router(agent_template_router)
    application.include_router(billing_router)
    return application


app = create_app()


@app.get("/healthz", tags=["operations"])
async def healthz():
    return {"status": "ok"}


@app.get("/readyz", tags=["operations"])
async def readyz(request: Request):
    state = request.app.state
    adapters = getattr(state, "channel_adapters", {})
    checks = {
        "authentication": getattr(state, "token_verifier", None) is not None,
        "api_key_directory": getattr(state, "api_key_directory", None) is not None,
        "agent_profile_directory": getattr(state, "agent_profile_directory", None) is not None,
        "billing": getattr(state, "subscription_authorizer", None) is not None,
        "voice_pipeline": getattr(state, "voice_pipeline_factory", None) is not None,
        "account_store": getattr(state, "account_store", None) is not None,
        "vertex_project_isolation": (
            not settings.vertex_project_isolation_enabled
            or getattr(state, "vertex_project_directory", None) is not None
        ),
        "provider_integration_store": getattr(state, "provider_integration_store", None) is not None,
        "object_storage": getattr(state, "object_storage_ready", True),
        "channel_adapter": bool(adapters),
        "post_call_agent": (
            not getattr(state, "post_call_required", True)
            or getattr(state, "post_call_agent", None) is not None
        ),
        "post_call_dispatcher": (
            not getattr(state, "post_call_required", True)
            or getattr(state, "post_call_dispatcher", None) is not None
        ),
    }
    ready = all(checks.values())
    response = {"status": "ready" if ready else "not_ready", "checks": checks}
    return response if ready else JSONResponse(status_code=503, content=response)
