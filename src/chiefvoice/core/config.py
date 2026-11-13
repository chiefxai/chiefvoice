from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "local")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    recordings_enabled: bool = os.getenv("RECORDINGS_ENABLED", "false").lower() == "true"
    recordings_dir: Path = Path(os.getenv("RECORDINGS_DIR", "./var/recordings"))
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
    vertex_credentials_json: str | None = os.getenv("VERTEX_CREDENTIALS_JSON") or None
    vertex_credentials_path: str | None = os.getenv("VERTEX_CREDENTIALS_PATH") or None
    post_call_agent_provider: str = os.getenv("POST_CALL_AGENT_PROVIDER", "langchain_vertex").strip().lower()
    post_call_model: str = os.getenv("POST_CALL_MODEL", "gemini-2.5-flash")
    post_call_encryption_key: str | None = os.getenv("POST_CALL_ENCRYPTION_KEY") or None
    vobiz_public_url: str | None = os.getenv("VOBIZ_PUBLIC_URL") or None
    vobiz_inbound_route_token: str | None = os.getenv("VOBIZ_INBOUND_ROUTE_TOKEN") or None
    vobiz_webhook_secret: str | None = os.getenv("VOBIZ_WEBHOOK_SECRET") or None
    vobiz_encoding: str = os.getenv("VOBIZ_ENCODING", "audio/x-mulaw")
    vobiz_sample_rate: int = int(os.getenv("VOBIZ_SAMPLE_RATE", "8000"))
    vobiz_record_inbound: bool = os.getenv("VOBIZ_RECORD_INBOUND", "false").lower() == "true"


settings = Settings()
