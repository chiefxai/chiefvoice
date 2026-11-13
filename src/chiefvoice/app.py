import logging
import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pipecat.pipeline.pipeline import Pipeline
from pipecat.transports.base_transport import BaseTransport

from chiefvoice.identity.auth import OidcTokenVerifier
from chiefvoice.identity.api_key_routes import router as api_key_router
from chiefvoice.identity.api_keys import SqlApiKeyDirectory
from chiefvoice.accounts.routes import router as accounts_router
from chiefvoice.accounts.store import SqlAccountStore
from chiefvoice.agents.call.configs.routes import router as agent_profiles_router
from chiefvoice.agents.call.configs.profiles import AgentProfileDirectory
from chiefvoice.agents.call.configs.store import SqlAgentProfileDirectory
from chiefvoice.agents.call.factory import create_realtime_pipeline_factory
from chiefvoice.agents.call.providers import RealtimeAgentRegistry
from chiefvoice.agents.call.providers.vertex import VertexGeminiLiveProvider
from chiefvoice.agents.post_call.providers.vertex import create_vertex_post_call_agent
from chiefvoice.agents.post_call.queue import PostCallDispatcher, SqlPostCallQueue
from chiefvoice.agents.post_call.queue import SqlPostCallWebhookDirectory
from chiefvoice.agents.post_call.webhook_routes import router as post_call_webhook_router
from chiefvoice.calls.coordinator import CallCoordinator
from chiefvoice.channels.base import ChannelAdapter
from chiefvoice.core.config import settings
from chiefvoice.observability import configure_logging, instrument_app
from chiefvoice.billing import SubscriptionAuthorizer
from chiefvoice.billing import BillingPolicyRegistry, PostpaidBillingPolicy, PrepaidBillingPolicy
from chiefvoice.billing.routes import router as billing_router
from chiefvoice.billing.store import SqlBillingDirectory
from chiefvoice.channels.vobiz.adapter import VobizAdapter
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
    StaticBackupPolicyDirectory,
    run_backups_periodically,
)
from chiefvoice.data_lifecycle.postgres_backup import EncryptedPostgresBackupProvider
from chiefvoice.sessions import AcceptedSession
from chiefvoice.calls.routes import router as calls_router

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    integration_store = getattr(_app.state, "provider_integration_store", None)
    account_store = getattr(_app.state, "account_store", None)
    api_key_directory = getattr(_app.state, "api_key_directory", None)
    post_call_webhook_directory = getattr(_app.state, "post_call_webhook_directory", None)
    agent_profile_directory = getattr(_app.state, "agent_profile_directory", None)
    post_call_dispatcher = getattr(_app.state, "post_call_dispatcher", None)
    post_call_agent = getattr(_app.state, "post_call_agent", None)
    billing_directory = getattr(_app.state, "billing_directory", None)
    worker_tasks: list[asyncio.Task] = []
    try:
        if account_store is not None:
            await account_store.initialize()
        if billing_directory is not None:
            await billing_directory.initialize()
        if integration_store is not None:
            await integration_store.initialize()
        if api_key_directory is not None:
            await api_key_directory.initialize()
        if post_call_webhook_directory is not None:
            await post_call_webhook_directory.initialize()
        if agent_profile_directory is not None:
            await agent_profile_directory.initialize()
        if isinstance(post_call_dispatcher, SqlPostCallQueue):
            await post_call_dispatcher.initialize()
        if isinstance(post_call_dispatcher, SqlPostCallQueue) and post_call_agent is not None:
            worker_tasks.append(asyncio.create_task(post_call_dispatcher.run_worker(post_call_agent)))
        retention_runner = getattr(_app.state, "retention_runner", None)
        if retention_runner is not None:
            worker_tasks.append(asyncio.create_task(
                run_retention_periodically(retention_runner, settings.retention_interval_seconds)
            ))
        backup_runner = getattr(_app.state, "backup_runner", None)
        if backup_runner is not None:
            worker_tasks.append(asyncio.create_task(
                run_backups_periodically(backup_runner, settings.backup_interval_seconds)
            ))
        logger.info("application_started", extra={"event": "application_started"})
        yield
    finally:
        for worker_task in worker_tasks:
            worker_task.cancel()
        for worker_task in worker_tasks:
            with suppress(asyncio.CancelledError):
                await worker_task
        if isinstance(post_call_dispatcher, SqlPostCallQueue):
            await post_call_dispatcher.close()
        if billing_directory is not None:
            await billing_directory.close()
        if integration_store is not None:
            await integration_store.close()
        if post_call_webhook_directory is not None:
            await post_call_webhook_directory.close()
        if agent_profile_directory is not None:
            await agent_profile_directory.close()
        if api_key_directory is not None:
            await api_key_directory.close()
        if account_store is not None:
            await account_store.close()
        logger.info("application_stopped", extra={"event": "application_stopped"})


def create_app(
    *,
    channel_adapters: Mapping[str, ChannelAdapter] | None = None,
    subscription_authorizer: SubscriptionAuthorizer | None = None,
    token_verifier: OidcTokenVerifier | None = None,
    provider_integration_store: SqlProviderIntegrationStore | None = None,
    account_store: SqlAccountStore | None = None,
    api_key_directory: SqlApiKeyDirectory | None = None,
    post_call_webhook_directory: SqlPostCallWebhookDirectory | None = None,
    agent_profile_directory: AgentProfileDirectory | None = None,
    billing_directory: SqlBillingDirectory | None = None,
    post_call_dispatcher: PostCallDispatcher | None = None,
    voice_pipeline_factory: Callable[
        [AcceptedSession, BaseTransport], Pipeline | Awaitable[Pipeline]
    ]
    | None = None,
) -> FastAPI:
    application = FastAPI(title="Chiefvoice", version="0.1.0", lifespan=lifespan)
    post_call_agent = None
    if (
        settings.post_call_agent_provider == "langchain_vertex"
        and settings.vertex_project_id
        and settings.vertex_location
    ):
        post_call_agent = create_vertex_post_call_agent(
            project_id=settings.vertex_project_id,
            location=settings.vertex_location,
            model=settings.post_call_model,
            credentials_json=settings.vertex_credentials_json,
            credentials_path=settings.vertex_credentials_path,
        )
    application.state.post_call_agent = post_call_agent
    post_call_required = settings.post_call_agent_provider != "disabled"
    if (
        post_call_dispatcher is None
        and post_call_agent is not None
        and settings.database_url
        and settings.post_call_encryption_key
    ):
        post_call_dispatcher = SqlPostCallQueue(
            settings.database_url, settings.post_call_encryption_key
        )
    application.state.post_call_dispatcher = post_call_dispatcher
    application.state.post_call_required = post_call_required
    if agent_profile_directory is None and settings.database_url:
        agent_profile_directory = SqlAgentProfileDirectory(settings.database_url)
    recording_store = create_recording_store()
    application.state.recording_store = recording_store
    if voice_pipeline_factory is None and settings.realtime_provider == "vertex":
        if settings.vertex_project_id and settings.vertex_location:
            vertex_provider = VertexGeminiLiveProvider(
                project_id=settings.vertex_project_id,
                location=settings.vertex_location,
                model=settings.vertex_model,
                voice=settings.vertex_voice,
                system_instruction=settings.vertex_system_instruction,
                profile_directory=agent_profile_directory,
                credentials_json=settings.vertex_credentials_json,
                credentials_path=settings.vertex_credentials_path,
            )
            voice_pipeline_factory = create_realtime_pipeline_factory(
                RealtimeAgentRegistry([vertex_provider]),
                "vertex",
                recording_store=recording_store,
            )
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
    if api_key_directory is None and settings.database_url:
        api_key_directory = SqlApiKeyDirectory(settings.database_url)
    if (
        post_call_webhook_directory is None
        and settings.database_url
        and settings.post_call_encryption_key
    ):
        post_call_webhook_directory = SqlPostCallWebhookDirectory(
            settings.database_url, settings.post_call_encryption_key
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
        provider = EncryptedPostgresBackupProvider(
            settings.database_url,
            settings.backup_directory,
            settings.backup_encryption_key,
            allowed_restore_targets=tuple(restore_targets),
        )
        backup_runner = BackupRunner(backup_policies, provider)
    vobiz_adapter = adapters.get("vobiz")
    if vobiz_adapter is None and settings.vobiz_webhook_secret and provider_integration_store is not None:
        vobiz_adapter = VobizAdapter(provider_integration_store)
        adapters["vobiz"] = vobiz_adapter
    application.state.provider_integration_store = provider_integration_store
    application.state.account_store = account_store
    application.state.billing_directory = billing_directory
    application.state.api_key_directory = api_key_directory
    application.state.post_call_webhook_directory = post_call_webhook_directory
    application.state.agent_profile_directory = agent_profile_directory
    application.state.retention_runner = retention_runner
    application.state.backup_runner = backup_runner
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
        )
        if subscription_authorizer is not None
        else None
    )
    application.state.voice_pipeline_factory = voice_pipeline_factory
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
    application.include_router(vobiz_router)
    application.include_router(integrations_router)
    application.include_router(api_key_router)
    application.include_router(post_call_webhook_router)
    application.include_router(agent_profiles_router)
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
        "provider_integration_store": getattr(state, "provider_integration_store", None) is not None,
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
