import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "local")
    object_storage_endpoint_url: str | None = os.getenv("OBJECT_STORAGE_ENDPOINT_URL") or None
    object_storage_region: str = os.getenv("OBJECT_STORAGE_REGION", "us-east-1")
    object_storage_bucket: str | None = os.getenv("OBJECT_STORAGE_BUCKET") or None
    object_storage_access_key_id: str | None = os.getenv("OBJECT_STORAGE_ACCESS_KEY_ID") or None
    object_storage_secret_access_key: str | None = os.getenv("OBJECT_STORAGE_SECRET_ACCESS_KEY") or None
    object_storage_session_token: str | None = os.getenv("OBJECT_STORAGE_SESSION_TOKEN") or None
    object_storage_addressing_style: str = os.getenv("OBJECT_STORAGE_ADDRESSING_STYLE", "path")
    object_storage_prefix: str = os.getenv("OBJECT_STORAGE_PREFIX", "chiefvoice").strip("/")
    recordings_storage_backend: str = os.getenv(
        "RECORDINGS_STORAGE_BACKEND",
        "local" if os.getenv("APP_ENV", "local").lower() == "local" else "s3",
    ).strip().lower()
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    recordings_enabled: bool = os.getenv("RECORDINGS_ENABLED", "false").lower() == "true"
    recordings_dir: Path = Path(os.getenv("RECORDINGS_DIR", "./var/recordings"))
    recordings_encryption_key: str | None = os.getenv("RECORDINGS_ENCRYPTION_KEY") or None
    recordings_allow_unencrypted_local: bool = os.getenv(
        "RECORDINGS_ALLOW_UNENCRYPTED_LOCAL",
        "true" if os.getenv("APP_ENV", "local").lower() == "local" else "false",
    ).lower() == "true"
    retention_interval_seconds: int = int(os.getenv("RETENTION_INTERVAL_SECONDS", "3600"))
    retention_policies_json: str = os.getenv("RETENTION_POLICIES_JSON", "[]")
    backup_directory: Path = Path(os.getenv("BACKUP_DIRECTORY", "./var/backups"))
    backup_storage_backend: str = os.getenv(
        "BACKUP_STORAGE_BACKEND",
        "local" if os.getenv("APP_ENV", "local").lower() == "local" else "s3",
    ).strip().lower()
    backup_encryption_key: str | None = os.getenv("BACKUP_ENCRYPTION_KEY") or None
    backup_policies_json: str = os.getenv("BACKUP_POLICIES_JSON", "[]")
    backup_interval_seconds: int = int(os.getenv("BACKUP_INTERVAL_SECONDS", "3600"))
    backup_restore_targets_json: str = os.getenv("BACKUP_RESTORE_TARGETS_JSON", "[]")
    otel_service_name: str = os.getenv("OTEL_SERVICE_NAME", "chiefvoice-api")
    otel_exporter_otlp_endpoint: str | None = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or None
    oidc_issuer: str | None = os.getenv("OIDC_ISSUER") or None
    oidc_audience: str | None = os.getenv("OIDC_AUDIENCE") or None
    oidc_jwks_url: str | None = os.getenv("OIDC_JWKS_URL") or None
    oidc_tenant_claim: str = os.getenv("OIDC_TENANT_CLAIM", "tenant_id")
    oidc_algorithms: tuple[str, ...] = tuple(
        algorithm.strip()
        for algorithm in os.getenv("OIDC_ALGORITHMS", "RS256").split(",")
        if algorithm.strip()
    )
    platform_admin_subjects: tuple[str, ...] = tuple(
        subject.strip()
        for subject in os.getenv("PLATFORM_ADMIN_SUBJECTS", "").split(",")
        if subject.strip()
    )
    database_url: str | None = os.getenv("DATABASE_URL") or None
    credentials_encryption_key: str | None = os.getenv("CREDENTIALS_ENCRYPTION_KEY") or None
    realtime_provider: str = os.getenv("REALTIME_AGENT_PROVIDER", "vertex").strip().lower()
    vertex_project_id: str | None = os.getenv("VERTEX_PROJECT_ID") or None
    vertex_location: str | None = os.getenv("VERTEX_LOCATION") or None
    vertex_model: str = os.getenv("VERTEX_MODEL", "google/gemini-live-2.5-flash-native-audio")
    vertex_voice: str = os.getenv("VERTEX_VOICE", "Charon")
    vertex_system_instruction: str = os.getenv(
        "VERTEX_SYSTEM_INSTRUCTION", "You are a helpful, concise voice assistant."
    )
    gemini_response_watchdog_seconds: float = float(
        os.getenv("GEMINI_RESPONSE_WATCHDOG_SECONDS", "3")
    )
    gemini_generation_watchdog_seconds: float = float(
        os.getenv("GEMINI_GENERATION_WATCHDOG_SECONDS", "8")
    )
    gemini_recovery_grace_seconds: float = float(
        os.getenv("GEMINI_RECOVERY_GRACE_SECONDS", "2")
    )
    # Independent caller silence and inactivity management configuration.
    caller_inactivity_timeout_seconds: float = float(
        os.getenv("CALLER_INACTIVITY_TIMEOUT_SECONDS", "30")
    )
    caller_inactivity_first_timeout_seconds: float = float(
        os.getenv("CALLER_INACTIVITY_FIRST_TIMEOUT_SECONDS", "10")
    )
    caller_inactivity_second_timeout_seconds: float = float(
        os.getenv("CALLER_INACTIVITY_SECOND_TIMEOUT_SECONDS", "10")
    )
    caller_inactivity_final_timeout_seconds: float = float(
        os.getenv("CALLER_INACTIVITY_FINAL_TIMEOUT_SECONDS", "8")
    )
    caller_inactivity_first_prompt: str = os.getenv(
        "CALLER_INACTIVITY_FIRST_PROMPT",
        "The caller has been silent. Ask briefly and politely if they are still there and can hear you.",
    )
    caller_inactivity_second_prompt: str = os.getenv(
        "CALLER_INACTIVITY_SECOND_PROMPT",
        "The caller is still silent. Inform them politely that you cannot hear them and will have to end the call if there is no response.",
    )
    caller_inactivity_goodbye_prompt: str = os.getenv(
        "CALLER_INACTIVITY_GOODBYE_PROMPT",
        "The caller has not responded. Politely say goodbye and thank them for calling.",
    )
    gemini_fallback_audio_path: Path | None = (
        Path(os.environ["GEMINI_FALLBACK_AUDIO_PATH"])
        if os.getenv("GEMINI_FALLBACK_AUDIO_PATH")
        else None
    )
    vertex_credentials_json: str | None = os.getenv("VERTEX_CREDENTIALS_JSON") or None
    vertex_credentials_path: str | None = os.getenv("VERTEX_CREDENTIALS_PATH") or None
    google_ai_api_key: str | None = os.getenv("GOOGLE_AI_API_KEY") or None
    google_ai_live_model: str = os.getenv("GOOGLE_AI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
    vertex_project_isolation_enabled: bool = os.getenv("VERTEX_PROJECT_ISOLATION_ENABLED", "false").lower() == "true"
    gcp_project_parent: str | None = os.getenv("GCP_PROJECT_PARENT") or None
    gcp_billing_account_id: str | None = os.getenv("GCP_BILLING_ACCOUNT_ID") or None
    gcp_runtime_service_account: str | None = os.getenv("GCP_RUNTIME_SERVICE_ACCOUNT") or None
    gcp_post_call_service_account: str | None = os.getenv("GCP_POST_CALL_SERVICE_ACCOUNT") or None
    gcp_cloud_tasks_service_account: str | None = os.getenv("GCP_CLOUD_TASKS_SERVICE_ACCOUNT") or None
    gcp_budget_api_project_id: str | None = os.getenv("GCP_BUDGET_API_PROJECT_ID") or None
    gcp_vertex_monthly_spend_cap: str | None = os.getenv("GCP_VERTEX_MONTHLY_SPEND_CAP") or None
    post_call_agent_provider: str = os.getenv("POST_CALL_AGENT_PROVIDER", "langchain_vertex").strip().lower()
    post_call_model: str = os.getenv("POST_CALL_MODEL", "gemini-3.5-flash-lite")
    post_call_location: str = os.getenv("POST_CALL_LOCATION", "global")
    post_call_encryption_key: str | None = os.getenv("POST_CALL_ENCRYPTION_KEY") or None
    post_call_execution_backend: str = os.getenv("POST_CALL_EXECUTION_BACKEND", "in_process").strip().lower()
    post_call_lambda_function_name: str | None = os.getenv("POST_CALL_LAMBDA_FUNCTION_NAME") or None
    post_call_lambda_region: str | None = os.getenv("POST_CALL_LAMBDA_REGION") or None
    post_call_sqs_queue_url: str | None = os.getenv("POST_CALL_SQS_QUEUE_URL") or None
    post_call_gcp_function_url: str | None = os.getenv("POST_CALL_GCP_FUNCTION_URL") or None
    post_call_cloud_tasks_queue: str | None = os.getenv("POST_CALL_CLOUD_TASKS_QUEUE") or None
    post_call_dispatch_interval_seconds: float = float(os.getenv("POST_CALL_DISPATCH_INTERVAL_SECONDS", "1"))
    post_call_redispatch_after_seconds: int = int(os.getenv("POST_CALL_REDISPATCH_AFTER_SECONDS", "120"))
    outbound_start_reconciliation_after_seconds: int = int(
        os.getenv("OUTBOUND_START_RECONCILIATION_AFTER_SECONDS", "120")
    )
    outbound_start_reconciliation_interval_seconds: int = int(
        os.getenv("OUTBOUND_START_RECONCILIATION_INTERVAL_SECONDS", "30")
    )
    vobiz_public_url: str | None = os.getenv("VOBIZ_PUBLIC_URL") or None
    vobiz_inbound_route_token: str | None = os.getenv("VOBIZ_INBOUND_ROUTE_TOKEN") or None
    vobiz_webhook_secret: str | None = os.getenv("VOBIZ_WEBHOOK_SECRET") or None
    vobiz_auth_id: str | None = os.getenv("VOBIZ_AUTH_ID") or None
    vobiz_auth_token: str | None = os.getenv("VOBIZ_AUTH_TOKEN") or None
    vobiz_encoding: str = os.getenv("VOBIZ_ENCODING", "audio/x-mulaw")
    vobiz_sample_rate: int = int(os.getenv("VOBIZ_SAMPLE_RATE", "8000"))
    vobiz_record_inbound: bool = os.getenv("VOBIZ_RECORD_INBOUND", "false").lower() == "true"


settings = Settings()
