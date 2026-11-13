# Chiefvoice

Provider-neutral real-time voice agent foundation using Pipecat.

## Start locally

Copy `.env.example` to `.env`, fill the settings you need, install [uv](https://docs.astral.sh/uv/), and run:

```sh
cp .env.example .env
uv sync
uv run uvicorn chiefvoice.app:app --reload --app-dir src --env-file .env --no-access-log
```

Database tables are managed with Alembic migrations, not created by application startup. Apply the schema before starting the app:

```sh
uv run alembic upgrade head
```

For an existing database created by an earlier `create_all` development build, inspect that its tables match `migrations/versions/0001_initial_schema.py` before stamping it with `uv run alembic stamp head`. Do not stamp an unverified database. Application startup checks that the database is at the current Alembic revision and fails with an actionable error if it is not.

The starter exposes `/healthz`, `/readyz`, and Prometheus metrics at `/metrics`. JSON logs go to stdout. Traces are created for FastAPI requests; set `OTEL_EXPORTER_OTLP_ENDPOINT` to export them over OTLP/HTTP.

## Authentication and subscriptions

Configure `OIDC_ISSUER`, `OIDC_AUDIENCE`, and `OIDC_JWKS_URL` to enable JWT verification against an OIDC provider. The tenant claim defaults to `tenant_id`; outbound calls require the `calls:write` scope. Calls are fail-closed until a `SubscriptionAuthorizer` and provider adapters are wired into the app. Each inbound adapter must authenticate its provider callback (a provider signature or a configured route secret) and resolve the provider account to a tenant before subscription checks.

Tenant API keys can be created only by an OIDC principal with `api_keys:write`; listing requires `api_keys:read`. Create with `POST /v1/api-keys` and pass only scopes already held by the OIDC principal. Keys use the `cv_live_` prefix, the full secret is returned once, and only its SHA-256 digest is stored. Revoke a key with `DELETE /v1/api-keys/{id}`. API keys cannot create, list, or revoke other API keys. Configure the API key directory with the shared PostgreSQL database and apply migrations before enabling the API-key router.

## Voice agent profiles

Create tenant-scoped live agent profiles with `POST /v1/agents` and `agents:write`; list and read with `agents:read`, update or archive with `agents:write`. A profile configures its name, supported language tag, Vertex voice ID, speaking style (`professional`, `friendly`, `casual`, or `slang`), and bounded agent instructions. Profile IDs are tenant-filtered on every read. Unknown or cross-tenant IDs are rejected before outbound dialing. Pass the selected profile ID as `bot_config_id` to `/v1/calls/outbound`; for inbound Vobiz calls, set the profile ID as the tenant integration's `settings.default_agent_id`. If neither is specified, the deployment's default Vertex voice and instruction are used. Supported language and voice IDs depend on the configured Vertex model.

```json
{
  "name": "Friendly English Support",
  "language": "en-US",
  "voice": "Charon",
  "speaking_style": "friendly",
  "instructions": "Keep answers concise. Confirm the customer's account before discussing account-specific details."
}
```

Start outbound calls with `POST /v1/calls/outbound`. New requests keep live conversation data separate from the post-call task and requested result shape. The live context is added as reference data to the selected live agent; the post-call task and schema are used only after hangup. Per-call context is encrypted in provider callback state and in the durable post-call queue. Combined contexts and schema are limited to 6,000 bytes.

```json
{
  "provider": "vobiz",
  "channel": "phone",
  "destination": "+15551234567",
  "live_agent": {
    "agent_id": "appointment-caller",
    "context": {
      "patient_name": "Alex",
      "appointment_date": "Tuesday"
    }
  },
  "post_call": {
    "context": "Record whether the patient confirmed or requested a new time. Do not guess if it was not discussed.",
    "output_schema": {
      "type": "object",
      "properties": {
        "outcome": {
          "type": "string",
          "enum": ["confirmed", "reschedule_requested", "unresolved"]
        },
        "notes": { "type": "string" }
      },
      "required": ["outcome", "notes"],
      "additionalProperties": false
    }
  }
}
```

The post-call schema accepts a bounded JSON Schema subset: objects, strings, integers, numbers, booleans, arrays, enums, and nested fields. It is compiled into a strict Pydantic model. LangChain structured output is validated against that model before the result is stored or delivered; invalid output retries through the durable post-call worker and is never sent as a successful result. This validates the response shape and allowed values, not factual truth, so prompts should request transcript-supported answers and encode an explicit unknown or unresolved value.

Billing abstractions live in `src/chiefvoice/billing/`: `BillingPolicyRegistry` selects the tenant's `PrepaidBillingPolicy` or `PostpaidBillingPolicy` from its account billing mode. The call coordinator now settles rounded-up elapsed seconds when a call ends; each ledger must make settlement idempotent by session ID and atomically close its reservation. The mode-specific `PrepaidLedger` and `PostpaidLedger` protocols still need concrete persistent implementations for balance reservation/debit, credit authorization, and usage accrual before calls can be enabled in production.

Billing mode is a tenant account property selected during account creation: `billing_mode` is either `prepaid` or `postpaid`. It is not selected per call. Prepaid calls are designed to reserve balance before provider call control and refund unused reservation; postpaid calls check account/credit status and accrue usage for invoicing. Reservation and settlement must be idempotent by session ID. A later billing-mode change should use an explicit, audited account update/migration flow. Billing-mode changes and concrete ledgers are not wired yet.

Create the authenticated tenant account with `POST /v1/accounts` and the `accounts:create` scope. The API takes `tenant_id` from the verified OIDC token and requires `display_name` and `billing_mode` in the request body. `GET /v1/accounts/current` with `accounts:read` returns the current tenant's account and billing mode. Creating the account does not itself activate a voice subscription; the subscription entitlement store remains a separate integration.

```json
{"display_name": "Acme Support", "billing_mode": "prepaid"}
```

## Audio recordings

Recordings are disabled by default. Enable them with `RECORDINGS_ENABLED=true`; the default Vertex pipeline then attaches `LocalRecordingStore`. Recording still requires the per-session subscription/consent decision (`recording_allowed=True`). The local store writes under a tenant-hashed directory with owner-only filesystem permissions. It is not encrypted and is for development only; production should provide a private encrypted object-store implementation of `RecordingStore` with retention, deletion, and access auditing.

The local store writes valid WAV segments under `RECORDINGS_DIR` (default `./var/recordings`). Do not put recordings or raw provider payloads in logs.

Cross-cutting data lifecycle contracts live in `src/chiefvoice/data_lifecycle/`. `RetentionPolicyDirectory` supplies tenant/data-class rules and `RetentionTarget` implementations purge expired records in bounded batches while respecting record-level legal holds. `BackupPolicyDirectory` and `BackupProvider` define scoped snapshots, encryption requirements, checksum/restore verification, and expiry. Concrete schedules, storage/database adapters, restore approvals, and retention audit events still need to be wired before production use.

## Realtime agent providers

The conversation engine has a provider boundary independent of the phone channel. Active-call agents live under `src/chiefvoice/agents/call/`; the first backend is Pipecat's Gemini Live speech-to-speech service on Vertex AI. Set `REALTIME_AGENT_PROVIDER=vertex`, `VERTEX_PROJECT_ID`, and `VERTEX_LOCATION`; authentication uses Google Application Default Credentials by default, or `VERTEX_CREDENTIALS_PATH` / `VERTEX_CREDENTIALS_JSON` for an explicit service account. Set `VERTEX_MODEL`, `VERTEX_VOICE`, and `VERTEX_SYSTEM_INSTRUCTION` as needed. On Google Cloud, grant the runtime service account Vertex AI access and enable the Vertex AI API.

The default app factory wires Vertex when project and location are configured. For another model provider, implement `RealtimeAgentProvider.create_processors(session)` in `agents/call/providers/` and register it through `RealtimeAgentRegistry`; `create_realtime_pipeline_factory(...)` keeps the transport, recording, and call lifecycle shared. This leaves room for an OpenAI/ChatGPT realtime backend without coupling it to Vobiz or other channels.

Post-call analysis lives in `agents/post_call/` and uses LangChain with a structured result (`summary`, `disposition`, `extracted_data`, `action_items`, and optional caller-defined `structured_output`). Its initial model is Gemini on Vertex AI, configured with the same Vertex project/location and credentials plus `POST_CALL_MODEL`. The live Pipecat pipeline collects Gemini Live caller and assistant transcription frames. At call end, a completed call with a transcript is inserted idempotently into the encrypted PostgreSQL `post_call_jobs` outbox; a background worker runs LangChain analysis off the hangup path and stores the encrypted result. Fetch job status and completed analysis with `GET /v1/calls/{session_id}/post-call` and the `calls:read` scope. Post-call analysis is required by default, so calls fail closed if the agent or durable queue is missing. To explicitly disable it, set `POST_CALL_AGENT_PROVIDER=disabled`. Apply the database migration and configure `POST_CALL_ENCRYPTION_KEY` (a dedicated Fernet key) to enable dispatch. Generate a key with `uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` and keep it separate from `CREDENTIALS_ENCRYPTION_KEY`. Jobs retry with backoff and a processing lease; monitor `chiefvoice_post_call_jobs_total{event="failed"}` and `chiefvoice_post_call_missing_transcripts_total`. Missing transcripts are logged without transcript content; other live providers must emit compatible Pipecat transcription/text frames or provide their own transcript collector.

## Post-call webhooks

Configure one result callback per tenant with `PUT /v1/webhooks/post-call` and `webhooks:write`, using `{"target_url":"https://crm.example.com/hooks/chiefvoice"}`. The endpoint must use HTTPS on port 443 and resolve only to public IP addresses. The response includes a generated signing secret once; it is encrypted at rest with `POST_CALL_ENCRYPTION_KEY`. `GET /v1/webhooks/post-call` shows the URL but never the secret, and `DELETE` disables future delivery and cancels pending deliveries.

After analysis completes, the worker sends `call.analysis.completed` with call metadata and structured results; it does not send the transcript. Delivery uses a durable outbox, stable event ID, HMAC-SHA256 signature, up to five attempts with backoff, and no redirects. The receiver should verify `X-Chiefvoice-Signature` over `timestamp + "." + event_id + "." + raw_request_body`, using the configured secret; reject timestamps outside a short replay window and deduplicate by `X-Chiefvoice-Event-Id`. Return any 2xx response to acknowledge receipt. Delivery is at least once, so receivers must tolerate retries. The polling endpoint remains available and includes webhook delivery status. DNS is checked both when configured and immediately before each attempt; these lookups are not pinned to the HTTP connection, so production deployments must also block private and link-local network egress.

## First channel: Vobiz

Vobiz uses its Voice API for outbound calls, VobizXML for the answer flow, and the official `pipecat-vobiz` serializer over a bidirectional WebSocket. The dependency is pinned to Vobiz's supported Pipecat 1.8 range. Configure `DATABASE_URL` and a Fernet `CREDENTIALS_ENCRYPTION_KEY` for tenant integration storage, plus `VOBIZ_PUBLIC_URL`, `VOBIZ_INBOUND_ROUTE_TOKEN`, and `VOBIZ_WEBHOOK_SECRET` for the shared webhook endpoint. Generate an encryption key with `uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` and store it in a secrets manager.

Configure each Vobiz number's inbound answer URL as `https://<public-host>/webhooks/vobiz/inbound?key=<VOBIZ_INBOUND_ROUTE_TOKEN>`. Provider settings use the shared integration API: `PUT /v1/integrations/{provider}` with `integrations:write`, `GET` with `integrations:read`, and `DELETE` with `integrations:write`. Start with `provider=vobiz`, credentials `auth_id` and `auth_token`, settings `default_caller_id`, and the account's phone numbers. Secrets are encrypted in the database and never returned by `GET`. Phone numbers are unique per provider so inbound routing resolves to one tenant. TeleCMI, Piopiy, and Twilio can use the same account storage/API as their adapters are added. Outbound requests use `/v1/calls/outbound` and require the `calls:write` OIDC scope plus an active voice entitlement.

Example Vobiz configuration body:

```json
{
  "credentials": {"auth_id": "your-auth-id", "auth_token": "your-auth-token"},
  "settings": {"default_caller_id": "+15551234567"},
  "phone_numbers": [
    {"number": "+15551234567", "inbound_enabled": true, "outbound_enabled": true}
  ]
}
```

The media endpoint is signed per-call and must be public over TLS. Generate random values for `VOBIZ_INBOUND_ROUTE_TOKEN` and `VOBIZ_WEBHOOK_SECRET` (for example, `openssl rand -hex 32`). Recordings stay disabled unless the global flag, subscription entitlement, and per-call decision all allow them; inbound recording has an additional `VOBIZ_RECORD_INBOUND=true` switch. Configure Vertex AI as described above to have the app build the realtime pipeline automatically, or pass a custom pipeline factory to `create_app`.

Access logs are disabled in the local command because Vobiz answer and media URLs carry time-limited signed state in their query string. Apply the same query-string redaction at any production proxy or load balancer.

## Current scope

The Vobiz control/media adapter, tenant-scoped provider integration store, shared session and pipeline boundaries, Vertex AI realtime provider, recording hook, health endpoints, structured logs, metrics, and request traces are in place. The subscription authorizer still needs a concrete entitlement store before live calls can run. Other channels and realtime model providers can follow their respective adapter boundaries. See [ARCHITECTURE.md](ARCHITECTURE.md) for the boundaries and sequence.
