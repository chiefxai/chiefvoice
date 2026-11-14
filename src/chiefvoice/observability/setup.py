import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import Counter, Histogram, make_asgi_app

from chiefvoice.core.config import settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("session_id", "channel", "provider", "event"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)


HTTP_REQUESTS = Counter(
    "chiefvoice_http_requests_total", "HTTP requests", ["method", "route", "status"]
)
HTTP_REQUEST_DURATION = Histogram(
    "chiefvoice_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "route"],
)
CALL_DURATION = Histogram(
    "chiefvoice_call_duration_seconds",
    "Completed or terminated voice call duration in seconds",
    ["channel", "provider", "reason"],
    buckets=(5, 15, 30, 60, 120, 300, 600, 1_200, 1_800, 3_600),
)
SESSIONS_STARTED = Counter(
    "chiefvoice_sessions_started_total", "Voice sessions started", ["channel", "provider"]
)
SESSIONS_ENDED = Counter(
    "chiefvoice_sessions_ended_total", "Voice sessions ended", ["channel", "provider", "reason"]
)
POST_CALL_MISSING_TRANSCRIPTS = Counter(
    "chiefvoice_post_call_missing_transcripts_total",
    "Completed calls that could not be queued because no transcript was captured",
    ["channel", "provider"],
)
POST_CALL_JOBS = Counter(
    "chiefvoice_post_call_jobs_total",
    "Post-call analysis jobs by lifecycle event",
    ["event"],
)
POST_CALL_WEBHOOKS = Counter(
    "chiefvoice_post_call_webhook_deliveries_total",
    "Post-call webhook delivery lifecycle events",
    ["event"],
)
CALL_STARTS = Counter(
    "chiefvoice_call_start_events_total",
    "Outbound call start outcomes",
    ["result"],
)
DATA_LIFECYCLE_RUNS = Counter(
    "chiefvoice_data_lifecycle_runs_total",
    "Retention and backup worker lifecycle events",
    ["task", "result"],
)
CALL_DURATION_LIMITS = Counter(
    "chiefvoice_call_duration_limit_events_total",
    "Calls that reached or exceeded their configured duration cap",
)
RECORDING_STORAGE_EVENTS = Counter(
    "chiefvoice_recording_storage_events_total",
    "Encrypted recording object storage outcomes",
    ["result"],
)


def session_started(channel: str, provider: str) -> None:
    SESSIONS_STARTED.labels(channel, provider).inc()


def call_start_event(result: str) -> None:
    if result in {"accepted", "rejected", "failed", "unavailable"}:
        CALL_STARTS.labels(result).inc()


def call_duration_limit_event() -> None:
    CALL_DURATION_LIMITS.inc()


def data_lifecycle_run(task: str, result: str) -> None:
    if task in {"backup", "retention"} and result in {"succeeded", "failed"}:
        DATA_LIFECYCLE_RUNS.labels(task, result).inc()


def recording_storage_event(result: str) -> None:
    if result in {"saved", "failed"}:
        RECORDING_STORAGE_EVENTS.labels(result).inc()


def session_ended(
    channel: str, provider: str, reason: str, duration_seconds: float | None = None
) -> None:
    safe_reason = (
        reason
        if reason in {"completed", "caller_hangup", "provider_error", "timeout", "cancelled"}
        else "other"
    )
    SESSIONS_ENDED.labels(channel, provider, safe_reason).inc()
    if duration_seconds is not None and duration_seconds >= 0:
        CALL_DURATION.labels(channel, provider, safe_reason).observe(duration_seconds)


def post_call_missing_transcript(channel: str, provider: str) -> None:
    POST_CALL_MISSING_TRANSCRIPTS.labels(channel, provider).inc()


def post_call_job_event(event: str) -> None:
    if event in {"enqueued", "completed", "retrying", "failed", "dispatched", "dispatch_failed"}:
        POST_CALL_JOBS.labels(event).inc()


def post_call_webhook_event(event: str) -> None:
    if event in {"queued", "delivered", "retrying", "failed", "cancelled"}:
        POST_CALL_WEBHOOKS.labels(event).inc()


def instrument_app(app: FastAPI) -> None:
    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "deployment.environment": settings.app_env,
        }
    )
    provider = TracerProvider(resource=resource)
    if settings.otel_exporter_otlp_endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    @app.middleware("http")
    async def record_http_metrics(request: Request, call_next):
        started = time.perf_counter()
        route_name = "unmatched"
        tracer = trace.get_tracer("chiefvoice.http")
        with tracer.start_as_current_span(
            f"HTTP {request.method}", kind=SpanKind.SERVER
        ) as span:
            # Deliberately omit URL/query attributes: provider callback URLs carry signed tokens.
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("http.route", route_name)
            try:
                response = await call_next(request)
            except Exception:
                route = request.scope.get("route")
                route_name = getattr(route, "path", route_name)
                span.set_status(Status(StatusCode.ERROR))
                span.set_attribute("http.route", route_name)
                HTTP_REQUESTS.labels(request.method, route_name, "500").inc()
                HTTP_REQUEST_DURATION.labels(request.method, route_name).observe(
                    time.perf_counter() - started
                )
                raise
            route = request.scope.get("route")
            route_name = getattr(route, "path", route_name)
            span.set_attribute("http.response.status_code", response.status_code)
            span.set_attribute("http.route", route_name)
            if response.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR))
            HTTP_REQUESTS.labels(request.method, route_name, str(response.status_code)).inc()
            HTTP_REQUEST_DURATION.labels(request.method, route_name).observe(
                time.perf_counter() - started
            )
            return response

    app.mount("/metrics", make_asgi_app())
