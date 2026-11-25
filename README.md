# Chiefvoice

Provider-neutral real-time voice agent foundation using Pipecat.

Provider deployment templates live in [`infra/`](infra/README.md). The primary AWS/GCP roots use a single VM for the API and PostgreSQL, provider-native post-call queues and functions, and S3-compatible object storage. Earlier managed-service layouts remain in `infra/scalable-infra/` for future use.

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

## Web console

A React operations console is in [`frontend/`](frontend/README.md). Start the API as above, then run `cd frontend && npm install && npm run dev`; open `http://localhost:5173` and connect with an OIDC access token. The console supports workspace and billing setup, voice agent profiles, Vobiz integration, outbound calls, and call status/post-call lookup. Call history is currently stored in the browser because the API has no tenant call-list endpoint.

### Run the console stack with Docker

Docker Compose starts PostgreSQL, applies migrations, launches the API and React console, and starts a local-only mock OIDC issuer. From the repository root:

```sh
cp .env.docker.example .env.docker
docker compose up --build
```

Open <http://localhost:5173>, choose **Sign in with OIDC**, and enter the local development credentials from [`frontend/README.md`](frontend/README.md) on the identity-provider page.

The token is valid for one hour and grants broad permissions only against the disposable local tenant `local-demo-tenant`. The mock issuer keeps its development signing key in a Docker volume across restarts. The compose setup disables cloud voice and post-call providers; it lets you test authentication, workspace and plan setup, agent profiles, and Vobiz integration storage, but placing a real call still requires Vobiz and Vertex AI credentials and a working media callback URL. Do not expose this development stack to a public network. Stop with `docker compose down`; add `-v` to also delete local PostgreSQL data and the mock OIDC signing key.

For an existing database created by an earlier `create_all` development build, inspect that its tables match `migrations/versions/0001_initial_schema.py` before stamping it with `uv run alembic stamp head`. Do not stamp an unverified database. Application startup checks that the database is at the current Alembic revision and fails with an actionable error if it is not.

The starter exposes `/healthz`, `/readyz`, and Prometheus metrics at `/metrics`. JSON logs go to stdout. Traces are created for FastAPI requests; set `OTEL_EXPORTER_OTLP_ENDPOINT` to export them over OTLP/HTTP.

## Authentication and subscriptions

Configure `OIDC_ISSUER`, `OIDC_AUDIENCE`, and `OIDC_JWKS_URL` to enable JWT verification against an OIDC provider. The tenant claim defaults to `tenant_id`; outbound calls require the `calls:write` scope. Calls are fail-closed until a `SubscriptionAuthorizer` and provider adapters are wired into the app. Each inbound adapter must authenticate its provider callback (a provider signature or a configured route secret) and resolve the provider account to a tenant before subscription checks.

The platform-admin flow provisions customers and their first API key with one database transaction. Set `PLATFORM_ADMIN_SUBJECTS` to a comma-separated allowlist of trusted OIDC `sub` values. Those principals also need the `platform:customers:write` or `platform:customers:read` scope. `POST /v1/admin/customers` creates a stable customer account and initial key; the full `cv_live_` key is returned once and only its SHA-256 digest is stored. Customer keys are scoped to a stable customer `tenant_id`, so rotating or adding a key does not move that customer's agents, phone numbers, calls, recordings, billing, or webhooks. Issue a rotation key with `POST /v1/admin/customers/{customer_id}/api-keys`; revoke the old one with `DELETE /v1/admin/customers/{customer_id}/api-keys/{key_id}`. Key scopes cannot grant platform administration, billing administration, or call reconciliation. `GET /v1/admin/customers` lists customer accounts. Tenant OIDC users may still use the legacy `/v1/api-keys` routes within their own account. Configure the API key directory with the shared PostgreSQL database and apply migrations before enabling the router.

## Voice agent profiles

Create tenant-scoped live agent profiles with `POST /v1/agents` and `agents:write`; list and read with `agents:read`, update or archive with `agents:write`. A profile configures its name, supported language tag, Vertex voice ID, speaking style (`professional`, `friendly`, `casual`, or `slang`), and bounded agent instructions. Profile IDs are tenant-filtered on every read. Unknown or cross-tenant IDs are rejected before outbound dialing. Pass the selected profile ID as `live_agent.agent_id` to `/v1/calls/outbound`; for inbound Vobiz calls, assign `agent_id` on each phone number in the provider integration. `settings.default_agent_id` remains the fallback for numbers without an explicit mapping. Supported language and voice IDs depend on the configured Vertex model.

```json
{
  "name": "Friendly English Support",
  "language": "en-US",
  "voice": "Charon",
  "speaking_style": "friendly",
  "instructions": "Keep answers concise. Confirm the customer's account before discussing account-specific details."
}
```

Start outbound calls with `POST /v1/calls/outbound`. New requests keep live conversation data separate from the post-call task and requested result shape. The optional `live_agent.language` BCP 47 tag selects the language for that call and overrides the selected agent profile or template language. The live context is added as reference data to the selected live agent; the post-call task and schema are used only after hangup. Per-call context is encrypted in provider callback state and in the durable post-call queue. Combined contexts and schema are limited to 6,000 bytes.

```json
{
  "provider": "vobiz",
  "channel": "phone",
  "destination": "+15551234567",
  "live_agent": {
    "agent_id": "appointment-caller",
    "language": "en-US",
    "context": {
      "patient_name": "Alex",
      "appointment_date": "Tuesday"
    }
  },
  "post_call": {
    "context": "Record whether the patient confirmed or requested a new time. Do not guess if it was not discussed.",
    "tools": ["calculator", "time"],
    "time_zone": "Asia/Kolkata",
    "time_format": "%Y-%m-%dT%H:%M:%S%z",
    "include_live_agent_context": true,
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

Post-call requests may select `calculator` and `time` in `post_call.tools`; only selected tools are exposed to the LangChain agent. The calculator evaluates bounded arithmetic locally without code execution. The time tool accepts an ISO 8601 timestamp with an explicit offset, converts it using `post_call.time_zone` (an IANA zone), and returns both `formatted` using `post_call.time_format` and an ISO 8601 value with the zone offset. The format must include `%z`. Defaults are `UTC` and `%Y-%m-%dT%H:%M:%S%z`. Set `include_live_agent_context` to `true` only when post-call extraction needs the live agent's context; otherwise that context is not copied into the post-call job or prompt.

## Reusable prompt templates (API-scoped)

For a guided real-call smoke test that creates API-assigned calling/post-call templates and starts a call in one command, see [`docs/cli-test-calls.md`](docs/cli-test-calls.md).

Chiefvoice supports reusable prompt templates scoped to individual API customers (`api_key_id`), keeping prompt libraries isolated and owned directly by each API rather than in a shared platform pool.

Templates come in two varieties:
1. **`calling` templates**: Configure the live real-time conversational agent instructions, tools (`end_call`), variables, and auto-hangup behavior.
2. **`post_call` templates**: Configure post-conversation structured information extraction, output schema, tools (`calculator`, `time`), timezone, and time format.

### Template management endpoints

Admin endpoints require platform administrator credentials (`platform:customers:write` / `platform:customers:read`):
- `POST /v1/admin/customers/api-keys/{key_id}/agent-templates`: Create a template owned by the API key (setting `is_default: true` configures it as default).
- `GET /v1/admin/customers/api-keys/{key_id}/agent-templates`: List all calling and post-call templates owned by this API key.
- `GET /v1/admin/customers/api-keys/{key_id}/agent-templates/{template_id}`: Retrieve a specific template.
- `PUT /v1/admin/customers/api-keys/{key_id}/agent-templates/{template_id}`: Update an existing template (increments revision).
- `DELETE /v1/admin/customers/api-keys/{key_id}/agent-templates/{template_id}`: Delete an owned template.
- `POST /v1/admin/customers/api-keys/{key_id}/agent-templates/{template_id}/default`: Set the active default template for this API.

### Placeholders and Variables

Placeholders enclosed in `{{...}}` inside instructions must be declared in the template's `variables` dictionary with their data type and required flag:

```json
{
  "kind": "calling",
  "name": "Appointment Check-In",
  "instructions": "You are calling {{customer_name}} regarding their appointment on {{appointment_date}}. Confirm attendance and answer questions.",
  "tools": [{"name": "end_call"}],
  "variables": {
    "customer_name": {"type": "string", "required": true},
    "appointment_date": {"type": "string", "required": true}
  },
  "settings": {
    "auto_hangup_when_complete": true
  },
  "is_default": true
}
```

When placing an outbound call via `POST /v1/calls/outbound`, if `live_agent.template_id` or `post_call.template_id` is omitted, the API's configured default template is automatically applied. Values supplied in `live_agent.context` and `post_call.context` are substituted into template placeholders at call start.

Billing abstractions live in `src/chiefvoice/billing/`. The PostgreSQL store reserves credit atomically per call, snapshots integer minor-unit rates, records idempotent prepaid debits or postpaid accruals, and caps reservations by the configured maximum call duration. `0004_voice_billing` creates the plan, reservation, and ledger tables. The first-writer answer timestamp is persisted so ring time is excluded from usage. Stale unanswered reservations are released; stale answered reservations close at their reserved cap so they stop holding credit and concurrency.

Billing mode is a tenant account property selected during account creation: `billing_mode` is either `prepaid` or `postpaid`. It is not selected per call. Configure the tenant's voice plan with `PUT /v1/billing/current` and the `billing:admin` scope; read it with `GET /v1/billing/current` and `billing:read`. The `billing:admin` scope must be held only by trusted operators and must never be issued to tenant users or tenant API keys: it can enable calls, set rates and limits, and grant prepaid balance or postpaid credit. Amounts are integers in the configured currency's minor unit, with one rate per minute and a maximum call duration.

Example prepaid plan body (for a `prepaid` account):

```json
{
  "voice_enabled": true,
  "currency": "USD",
  "rate_per_minute_minor": 25,
  "max_call_seconds": 300,
  "allowed_channels": ["phone"],
  "max_concurrent_sessions": 2,
  "recording_allowed": false,
  "prepaid_balance_minor": 5000
}
```

This ledger is internal usage accounting only. It does not charge a card, accept payments, create invoices, or perform currency conversion. Postpaid usage accumulates against the configured credit limit; an operator must reconcile payments and adjust the limit until a payment/invoicing integration is added. If a call provider fails to report the end event, the reconciler caps answered usage at its reserved maximum; actual usage beyond that cap is not billed through this ledger.

Provision a customer through the platform-admin API with `POST /v1/admin/customers`, passing `display_name`, `billing_mode`, and optional initial API-key scopes. The server generates a customer ID and atomically creates the account with its first API key; the secret is shown once. Account creation does not itself activate a voice subscription; the subscription entitlement store remains a separate integration. The older `POST /v1/accounts` route remains for OIDC tenants that manage their own single account.

```json
{"display_name": "Acme Support", "billing_mode": "prepaid"}
```

An async Python SDK is available at `src/chiefvoice_sdk`. Outbound creation accepts an optional `Idempotency-Key` header (1–200 characters), scoped to the tenant. Reusing the same key and same request returns the original call session; reusing it for a different request returns `409`. Use a fresh key for an intentional new call. When all concurrent call channels are occupied, a valid outbound request is accepted with status `queued`; the durable FIFO worker dials it when a slot becomes available. Queued calls expire after 15 minutes and can be cancelled with `POST /v1/calls/{session_id}/cancel` using `calls:write`. Calls waiting in the queue do not reserve billing funds or a channel. Inactive plans, unsupported channels, and insufficient credit remain immediate errors. The key is durable in Postgres, so the platform will not dial twice after a client retry or process restart. A provider timeout with an uncertain outcome is kept as `reconciliation_required`; the platform does not automatically retry the dial and keeps the billing reservation until an operator verifies the provider's call history. If an outbound provider sends a hangup callback before the answer callback, the call is marked `failed` with `provider_hangup_before_answer`, its billing reservation is released, and Chiefvoice emits `call.failed` rather than attempting post-call analysis. The stale-start worker flags abandoned `starting` rows after `OUTBOUND_START_RECONCILIATION_AFTER_SECONDS` (default 120 seconds). A principal with `calls:reconcile` can resolve via `POST /v1/calls/{session_id}/reconcile`, setting `confirmed_started` and `provider_call_id`, or confirming `confirmed_started: false` to release the reservation. Query `GET /v1/calls/{session_id}` with `calls:read` to poll call completion, `GET /v1/calls/{session_id}/transcript` to retrieve the encrypted-at-rest transcript after it is persisted, and `GET /v1/calls/{session_id}/post-call` to retrieve post-call status and results. The transcript endpoint returns `transcript: null` and `transcript_available: false` when no transcript has been saved (for example, when the call was not answered); transcript text is returned only to a caller authorized for that call and is marked `Cache-Control: private, no-store`.

## Audio recordings

Recordings are disabled by default. Enable them with `RECORDINGS_ENABLED=true`; recording still requires the per-session subscription/consent decision (`recording_allowed=True`). Local development defaults to `RECORDINGS_STORAGE_BACKEND=local`; non-local environments default to S3-compatible storage. S3 recording objects and their metadata are client-side encrypted with `RECORDINGS_ENCRYPTION_KEY`, tenant-isolated under a hashed key prefix, and written without public ACLs. Plain WAV files are allowed only in local development when `RECORDINGS_ALLOW_UNENCRYPTED_LOCAL=true`. An authenticated tenant can download a call recording with `GET /v1/calls/{session_id}/recording` and `calls:read`; the API verifies call ownership and storage metadata, decrypts the audio, and streams it without exposing storage keys. Downloads are limited to 128 MiB. Because objects are client-side encrypted, the service does not issue direct object-store URLs.

Set `OBJECT_STORAGE_BUCKET` for the bucket and `OBJECT_STORAGE_PROVIDER` to `gcs` or `s3`. GCP deployments should use `OBJECT_STORAGE_PROVIDER=gcs`; the native Google Cloud Storage SDK obtains credentials from Application Default Credentials (ADC) on the attached service account, without HMAC keys or S3 credentials. For S3-compatible services, use `OBJECT_STORAGE_PROVIDER=s3`; set `OBJECT_STORAGE_ENDPOINT_URL` for custom endpoints and `OBJECT_STORAGE_REGION`, with optional access-key/secret settings or the runtime credential chain. `OBJECT_STORAGE_PREFIX=chiefvoice` scopes this app's keys. Recordings use the `recordings/` prefix; backups use `backups/`. The bucket must be private and permit list/get/put/delete for those prefixes. `RECORDINGS_DIR` remains the local-development path. Do not put recordings or raw provider payloads in logs.

Example S3-compatible configuration:

```dotenv
OBJECT_STORAGE_ENDPOINT_URL=https://objects.example.com
OBJECT_STORAGE_REGION=us-east-1
OBJECT_STORAGE_BUCKET=chiefvoice-prod
OBJECT_STORAGE_ACCESS_KEY_ID=...
OBJECT_STORAGE_SECRET_ACCESS_KEY=...
OBJECT_STORAGE_ADDRESSING_STYLE=path
OBJECT_STORAGE_PREFIX=chiefvoice
RECORDINGS_STORAGE_BACKEND=s3
```

Cross-cutting data lifecycle code lives in `src/chiefvoice/data_lifecycle/`. Explicit `RETENTION_POLICIES_JSON` settings start a bounded background purge worker for recordings and post-call transcript/result records. Empty policy lists disable deletion. Legal holds are honored for encrypted recordings; transcript retention skips active leased jobs. Policies are deployment configuration today, not yet tenant-editable UI/API, and supported data classes depend on wired storage targets.

PostgreSQL backups can be enabled with `BACKUP_POLICIES_JSON`, `BACKUP_ENCRYPTION_KEY`, and a configured `DATABASE_URL`. Non-local environments default to `BACKUP_STORAGE_BACKEND=s3`; local development defaults to `local`. S3 backups are AES-GCM encrypted before upload, metadata is authenticated, and verification downloads/decrypts the archive and checks its checksum and PostgreSQL archive structure. The local `BACKUP_DIRECTORY` is used only as encrypted staging for S3 uploads and restore verification. The runtime image must include compatible `pg_dump` and `pg_restore` binaries. `BACKUP_RESTORE_TARGETS_JSON` is an optional JSON list of exact DSNs permitted for restore; restoring to the source database is rejected. Backups are service-scoped because tenants share a database. Align backup expiry with storage lifecycle policy and run scheduled restore drills. Recording retention is enforced by the app worker; do not configure bucket lifecycle rules that delete recordings before that worker can respect configured holds. S3 Object Lock is not configured by this app. No live backup or restore was run for this code change.

Retention and backup worker outcomes, plus recording upload success/failure, are emitted as low-cardinality Prometheus metrics. Example dashboard and alert rules are in `monitoring/`; import/configure them in your Prometheus/Grafana stack. These files do not provision monitoring infrastructure.

## Realtime agent providers

The conversation engine has a provider boundary independent of the phone channel. Active-call agents live under `src/chiefvoice/agents/call/`; the first backend is Pipecat's Gemini Live speech-to-speech service. Agent profiles can use Tamil (`ta-IN`), Malayalam (`ml-IN`), and Telugu (`te-IN`) with selectable Gemini Live voices and language-aware mild slang. Google's published Vertex Live language list includes Tamil and Telugu but does not currently list Malayalam, so Malayalam profiles are routed to the Google AI Gemini Live API. Set `GOOGLE_AI_API_KEY` (and optionally `GOOGLE_AI_LIVE_MODEL`, default `gemini-3.1-flash-live-preview`) to enable Malayalam. This uses separate Google AI API credentials, billing, and quota from Vertex AI; it does not inherit tenant Vertex project isolation. Without the key, Malayalam calls fail with an explicit setup error. Tamil and Telugu continue to use Vertex. For those, set `REALTIME_AGENT_PROVIDER=vertex`, `VERTEX_PROJECT_ID`, and `VERTEX_LOCATION`; authentication uses Google Application Default Credentials by default, or `VERTEX_CREDENTIALS_PATH` / `VERTEX_CREDENTIALS_JSON` for an explicit service account. Set `VERTEX_MODEL`, `VERTEX_VOICE`, and `VERTEX_SYSTEM_INSTRUCTION` as needed. On Google Cloud, grant the runtime service account Vertex AI access and enable the Vertex AI API. See Google's [Vertex Live language and voice configuration](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/configure-language-voice?hl=en), [Gemini API Live language capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities?authuser=14), and [current Gemini API models](https://ai.google.dev/gemini-api/docs/models).

Inbound caller PCM is amplified before resampling and model input, while assistant output is unchanged. `CALLER_AUDIO_GAIN_DB` controls the gain (default `6`, allowed range `0`–`24`); sample peaks are saturated to prevent integer wraparound, which can cause distortion at loud peaks. Gain also raises background noise, so adjust it against real call audio if callers report noise or distortion. Phone calls use local Silero VAD to confirm Gemini interruption events before stopping assistant audio. Gemini server VAD starts conservatively and uses a 700 ms silence threshold to avoid chopping normal pauses. The call logs `caller_speech_detected_by_local_vad`, `gemini_interruption_suppressed_without_local_speech`, and related counters in `call_audio_flow_summary`. This guards against false barge-ins but is not acoustic echo cancellation: echo returned by a carrier/handset that also looks like sustained caller speech may still need echo cancellation on the telephony path. A per-turn response watchdog starts after the caller finishes speaking. It is cancelled when caller speech, Gemini generation, or bot audio playback begins. If Gemini does not respond, the watchdog checks connection readiness and attempts one controlled recovery.

Audio diagnostics count caller and assistant audio at the Vobiz, Gemini, and serialization boundaries without storing audio content. `gemini_audio_buffer_replayed` reports flushing caller audio queued while the Gemini session was unavailable; it is not an assistant playback-buffer event. The `gemini_audio_frames` summary separates frames received, queued, held in local-VAD pre-roll, sent, and dropped. Queued frames are later counted as sent when flushed, so these values are flow counters and should not be summed as unique frames. Playback checkpoints are sent after the transport drains a bot turn; `vobiz_playback_ack_received.latency_ms` measures the provider's playback acknowledgement, and `vobiz_playback_ack_timeout` indicates no acknowledgement before the configured timeout. A final checkpoint send failure after the media WebSocket disconnects is a teardown event, not proof of an in-call playback backlog.

To inspect one call on a VM running the Compose service:

```sh
SID='your-session-id'
sudo docker logs --since 24h --timestamps chiefvoice-api-1 2>&1 \
  | grep -F "$SID" \
  | grep -E 'call_audio_flow_summary|gemini_audio_buffer|gemini_audio_dropped|gemini_audio_send_failed|gemini_audio_frame_rejected|gemini_audio_buffer_evicted_frames|gemini_session_readiness_changed|vobiz_playback|caller_speech|gemini_generation'
```

Use the per-stage audio frame and byte totals together with drop/send-failure events. Playback acknowledgements show that the provider consumed audio through the checkpoint, but do not measure the handset's acoustic output or carrier-side buffering. Older deployed images may omit detailed queue-depth fields; compare the running image with its Cloud Build source revision before relying on those fields.

Caller silence management provides staged follow-up prompts: after configurable inactivity intervals (`CALLER_INACTIVITY_FIRST_TIMEOUT_SECONDS`, `CALLER_INACTIVITY_SECOND_TIMEOUT_SECONDS`), the agent prompts the caller with follow-ups. If caller speech resumes at any point, timers and stages are reset. If silence continues after the policy completes, the call terminates gracefully.

Call termination execution (`hang_up_call`) is secured with server-side completion validation: fields explicitly required by the live-call completion contract or session metadata are verified before normal completion; post-call output schemas describe later extraction and do not by themselves block live hangup. Premature hang-up requests (such as before any caller interaction or while live-required fields are missing) are rejected with actionable tool error messages so the agent can continue collecting required information. Furthermore, closing farewell audio is drained before telephony disconnection, and termination is idempotent across multiple tool calls, webhooks, and media disconnections.

To run end-to-end outbound call validation (check-in, check-out, long pauses, delayed Gemini responses, recordings, transcripts, and structured termination logs):
```bash
./.venv/bin/python scripts/validate_outbound_calls.py
```

For per-customer Google Cloud project isolation, enable `VERTEX_PROJECT_ISOLATION_ENABLED=true` and configure `GCP_PROJECT_PARENT`, `GCP_BILLING_ACCOUNT_ID`, `GCP_RUNTIME_SERVICE_ACCOUNT`, `GCP_BUDGET_API_PROJECT_ID`, and `GCP_VERTEX_MONTHLY_SPEND_CAP`. `DATABASE_URL` and `VERTEX_LOCATION` are also required. The configured service identity uses Application Default Credentials. Grant it project creation on the selected organization/folder, billing account permissions to attach projects and create/configure spend cap budgets, project service-usage and IAM administration permissions for the new projects, and permission to use `GCP_BUDGET_API_PROJECT_ID` for budget API quota attribution. Enable Resource Manager, Cloud Billing, Service Usage, Vertex AI, and Cloud Billing Budget APIs where required. The billing budget API consumer project must have `billingbudgets.googleapis.com` enabled. The platform provisions a separate tenant project asynchronously during account creation, links it to the platform billing account, enables Vertex AI, grants the platform runtime service account `roles/aiplatform.user`, and creates a monthly Vertex-only spend cap. The runtime sends the project ID from that tenant's database row to both live-call Vertex and post-call Vertex. No service-account private key is returned or stored by this flow; use workload identity/ADC and rotate credentials through your cloud environment.

Account creation returns a `vertex_project` object with provisioning `status` (`pending`, `provisioning`, `ready`, or `failed`). Check `GET /v1/accounts/current/vertex-project` for status; retry a terminal failure with `POST /v1/accounts/current/vertex-project/retry`. Outbound calls are rejected before dialing until the tenant project is ready. Apply migration `0006_tenant_vertex_projects` before enabling project isolation. Configure `GCP_VERTEX_MONTHLY_SPEND_CAP` through deployment configuration for your initial monthly per-customer cap. Google documents spend caps as estimated, delayed enforcement; in-flight usage and reporting delays can cause the final amount to exceed the configured cap. See [Cloud Billing spend caps](https://cloud.google.com/billing/docs/how-to/budgets-spend-caps), [project creation permissions](https://cloud.google.com/resource-manager/docs/creating-managing-projects), and [service account best practices](https://cloud.google.com/iam/docs/best-practices-service-accounts).

The default app factory wires Vertex when project and location are configured. For another model provider, implement `RealtimeAgentProvider.create_processors(session)` in `agents/call/providers/` and register it through `RealtimeAgentRegistry`; `create_realtime_pipeline_factory(...)` keeps the transport, recording, and call lifecycle shared. This leaves room for an OpenAI/ChatGPT realtime backend without coupling it to Vobiz or other channels.

Post-call analysis lives in `agents/post_call/` and uses LangChain with a structured result (`summary`, `disposition`, `extracted_data`, `action_items`, and optional caller-defined `structured_output`). Its initial model is Gemini on Vertex AI, configured with the same Vertex project/location and credentials plus `POST_CALL_MODEL`. The live Pipecat pipeline collects Gemini Live caller and assistant transcription frames. At call end, a completed call with a transcript is inserted idempotently into the encrypted PostgreSQL `post_call_jobs` outbox; the default `in_process` worker runs analysis off the hangup path and stores the encrypted result. Fetch job status and completed analysis with `GET /v1/calls/{session_id}/post-call` and the `calls:read` scope. Post-call analysis is required by default, so calls fail closed if the agent or durable queue is missing. To explicitly disable it, set `POST_CALL_AGENT_PROVIDER=disabled`. Apply database migrations and configure `POST_CALL_ENCRYPTION_KEY` (a dedicated Fernet key) to enable dispatch. Generate a key with `uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` and keep it separate from `CREDENTIALS_ENCRYPTION_KEY`. Jobs retry with backoff and a processing lease; monitor `chiefvoice_post_call_jobs_total{event="failed"}` and `chiefvoice_post_call_missing_transcripts_total`. Missing transcripts are logged without transcript content; other live providers must emit compatible Pipecat transcription/text frames or provide their own transcript collector.

Recording review uses a separate Google Cloud Speech-to-Text model (`POST_CALL_TRANSCRIPTION_MODEL=chirp_2`, `POST_CALL_TRANSCRIPTION_LOCATION=asia-southeast1`) when the first analysis flags unclear speech. Tamil (`ta-*`) calls are also reviewed automatically when a recording is available. The independent transcript is passed alongside the original labeled live transcript; the analysis must compare evidence and keep uncertain facts unknown. Configure `POST_CALL_TRANSCRIPTION_PROJECT_ID` or let it fall back to `VERTEX_PROJECT_ID`/Application Default Credentials. Enable the Speech-to-Text API and grant the post-call worker service account `roles/speech.client` on that project. The Cloud Speech transcript is unlabeled, so speaker attribution continues to rely on the live transcript. Calls without usable recordings retain the original analysis.

For AWS Lambda execution, set `POST_CALL_EXECUTION_BACKEND=lambda` and `POST_CALL_LAMBDA_FUNCTION_NAME` on the API. The API's PostgreSQL outbox remains the source of truth; it invokes Lambda asynchronously with only `{schema_version, job_id}`. Set the handler to `chiefvoice.agents.post_call.lambda_handler.handler`, and give that function access to the same `DATABASE_URL`, `POST_CALL_ENCRYPTION_KEY`, Vertex settings/credentials, and database migrations. The API role needs `lambda:InvokeFunction`; the Lambda role needs database network access and Vertex credentials. Apply migration `0005_post_call_lambda_dispatch` before enabling this mode. The dispatcher resends jobs that were not claimed before `POST_CALL_REDISPATCH_AFTER_SECONDS` and expired processing leases, so dropped/failed Lambda events can be recovered from Postgres. A lease and idempotent job claim make duplicate Lambda events safe. The execution adapter is isolated in `agents/post_call/lambda_dispatch.py`; another compute provider can implement the same `invoke(job_id)` interface later. CRM result webhooks continue to be delivered from the API's persisted webhook outbox.

For the production GCP deployment, the API runs on a Compute Engine VM and dispatches durable post-call jobs through Cloud Tasks to an authenticated Cloud Functions 2nd gen (Cloud Run functions) endpoint. The API sends only `{schema_version, job_id}`; the function claims and processes the job from the same PostgreSQL outbox. Duplicate or late requests safely no-op, and failed jobs use the durable retry policy. PostgreSQL runs on the backend VM, and the function connects through the Terraform-managed VPC connector. Cloud Build deploys the function, grants the Cloud Tasks invoker service account `roles/run.invoker`, retrieves its URL, and supplies that URL to the VM deployment before the API restarts. The function identity receives Secret Manager access to its database, post-call, recording, and object-storage credentials plus Vertex AI access. See the active [GCP infrastructure deployment guide](https://github.com/chiefxai/chiefvoice-infra/tree/main/gcp). The separate `deploy/gcp/` Cloud Run/Cloud SQL template is a legacy deployment path, not the production architecture.

Build the lightweight Lambda container from `deploy/aws/post-call-lambda/` and apply the least-privilege network/IAM setup described in its README.

If `OBJECT_STORAGE_BUCKET` is configured, a Postgres enqueue failure falls back to an encrypted S3-compatible `post-call-recovery/` spool. The API periodically reconciles staged calls into the Postgres outbox and deletes an object only after insertion or duplicate confirmation. Do not configure an object-lifecycle expiry on this prefix while records are pending. Without that bucket, enqueue failure is logged and counted but cannot be recovered by this mechanism; alert on `chiefvoice_post_call_enqueue_fallback_total`.

## Post-call webhooks

Configure one result callback per tenant with `PUT /v1/webhooks/post-call` and `webhooks:write`, using `{"target_url":"https://crm.example.com/hooks/chiefvoice"}`. The endpoint must use HTTPS on port 443 and resolve only to public IP addresses. The response includes a generated signing secret once; it is encrypted at rest with `POST_CALL_ENCRYPTION_KEY`. `GET /v1/webhooks/post-call` shows the URL but never the secret, and `DELETE` disables future delivery and cancels pending deliveries.

After analysis completes, the worker sends `call.analysis.completed` with call metadata and structured results; it does not send the transcript. Chiefvoice also sends `call.failed` for call setup/live media failures, including an outbound provider hangup before answer, and `call.post_call.failed` when post-call processing cannot complete for an answered call, such as exhausted analysis retries or unavailable transcript/recording. Failure events are built from platform state without invoking AI and contain a stable `failure.stage`, `failure.code`, retryability flag, and attempt count—not raw exception text or transcript content. These callbacks are delivered even when no post-call agent is configured, as long as a webhook URL is configured.

All events use a durable outbox, stable event ID, HMAC-SHA256 signature, up to five attempts with backoff, and no redirects. The receiver should verify `X-Chiefvoice-Signature` over `timestamp + "." + event_id + "." + raw_request_body`, using the configured secret; reject timestamps outside a short replay window and deduplicate by `X-Chiefvoice-Event-Id`. Return any 2xx response to acknowledge receipt. Delivery is at least once, so receivers must tolerate retries. The polling endpoint remains available and includes webhook delivery status. DNS is checked both when configured and immediately before each attempt; these lookups are not pinned to the HTTP connection, so production deployments must also block private and link-local network egress.

Failed analysis can be queued again with `POST /v1/calls/{session_id}/post-call/retry` using `calls:write`. Failed callback delivery can be retried with `POST /v1/webhooks/post-call/deliveries/{session_id}/retry` using `webhooks:write`. A failed callback retry uses the tenant's currently configured URL and signing secret because the failed delivery's old target and secret were erased; it keeps the original event ID so receivers can deduplicate. Both operations are tenant-scoped and return `409` when the job is not in a retryable state.

## First channel: Vobiz

Vobiz uses its Voice API for outbound calls, VobizXML for the answer flow, and the official `pipecat-vobiz` serializer over a bidirectional WebSocket. The dependency is pinned to Vobiz's supported Pipecat 1.8 range. Configure `DATABASE_URL` and a Fernet `CREDENTIALS_ENCRYPTION_KEY` for tenant integration storage, plus `VOBIZ_PUBLIC_URL`, `VOBIZ_INBOUND_ROUTE_TOKEN`, and `VOBIZ_WEBHOOK_SECRET` for the shared webhook endpoint. `VOBIZ_MEDIA_START_TIMEOUT_SECONDS` bounds the wait for Vobiz's WebSocket start message (default 8 seconds). Timeout or malformed start data is logged with its precise startup stage, terminates the call, and emits a provider failure event rather than leaving the caller connected to a silent session. Generate an encryption key with `uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` and store it in a secrets manager.

Configure each Vobiz number's inbound answer URL as `https://<public-host>/webhooks/vobiz/inbound?key=<VOBIZ_INBOUND_ROUTE_TOKEN>`. Provider settings use the shared integration API: `PUT /v1/integrations/{provider}` with `integrations:write`, `GET` with `integrations:read`, and `DELETE` with `integrations:write`. Start with `provider=vobiz`, credentials `auth_id` and `auth_token`, settings `default_caller_id`, and the account's phone numbers. Each number may set an `agent_id` for inbound routing; otherwise the integration's `default_agent_id` is used. Profile IDs are validated against the authenticated customer. Secrets are encrypted in the database and never returned by `GET`. Phone numbers are unique per provider so inbound routing resolves to one tenant. TeleCMI, Piopiy, and Twilio can use the same account storage/API as their adapters are added. Outbound requests use `/v1/calls/outbound` and require a customer API key with `calls:write` (or an OIDC token with that scope) plus an active voice entitlement.

Example Vobiz configuration body:

```json
{
  "credentials": {"auth_id": "your-auth-id", "auth_token": "your-auth-token"},
  "settings": {"default_caller_id": "+15551234567"},
  "phone_numbers": [
    {"number": "+15551234567", "inbound_enabled": true, "outbound_enabled": true, "agent_id": "agent-profile-id"}
  ]
}
```

The media endpoint is signed per-call and must be public over TLS. Generate random values for `VOBIZ_INBOUND_ROUTE_TOKEN` and `VOBIZ_WEBHOOK_SECRET` (for example, `openssl rand -hex 32`). Inbound requests must include Vobiz V2/V3 HMAC headers signed with the tenant integration's auth token; V3 is preferred. The callback nonce is recorded in Postgres and rejected if reused during the 24-hour replay window. Run `alembic upgrade head` before enabling inbound traffic. Inbound call IDs are persisted before admission, and provider retries reuse the same call session and billing reservation. Recordings stay disabled unless the global flag, subscription entitlement, and per-call decision all allow them; inbound recording has an additional `VOBIZ_RECORD_INBOUND=true` switch. Configure Vertex AI as described above to have the app build the realtime pipeline automatically, or pass a custom pipeline factory to `create_app`.

Access logs are disabled in the local command because Vobiz answer and media URLs carry time-limited signed state in their query string. Apply the same query-string redaction at any production proxy or load balancer.

## Current scope

For the production Compute Engine, VM-hosted PostgreSQL, Cloud Tasks, and Cloud Run functions deployment, see the [GCP infrastructure guide](https://github.com/chiefxai/chiefvoice-infra/tree/main/gcp). `deploy/gcp/README.md` documents a legacy Cloud Run/Cloud SQL layout and is not the current deployment path.

The Vobiz control/media adapter, tenant-scoped provider integration store, shared session and pipeline boundaries, Vertex AI realtime provider, recording hook, prepaid/postpaid usage ledgers, lifecycle workers, health endpoints, structured logs, metrics, and request traces are in place. This remains an integration foundation, not a hosted production service: configure real identity, database, Vobiz and Vertex credentials, subscription entitlements, provider pricing, secrets management, storage, and monitoring infrastructure before customer traffic. Inbound is scaffolded, while the current product focus is outbound. Other channels and realtime model providers can follow their respective adapter boundaries. See [ARCHITECTURE.md](ARCHITECTURE.md) for the boundaries and sequence.
