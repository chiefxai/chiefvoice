# Voice agent architecture

## Decision

Use Pipecat as the real-time conversation runtime. Keep provider-specific call setup, webhooks, media framing, and call controls behind channel adapters. A provider adapter must not contain the agent's business logic.

## Main boundary

```text
Vobiz PSTN ─────────────┐
Browser / Mobile WebRTC ├─> Channel adapter ─> normalized session ─> Pipecat pipeline
Future providers ───────┘                                             │
                                                                      └─> tools / app services
```

There are two separate concerns at the edge:

- **Control plane:** inbound call webhooks, authentication/signature checks, answer/reject, outbound dialing, hangup, transfer, and provider callbacks.
- **Media plane:** WebRTC or WebSocket audio, codec conversion, packet timing, DTMF, and disconnect detection.

The shared session layer joins those concerns and supplies normalized call metadata to the bot. The Pipecat pipeline owns audio processing, turn taking, interruption handling, STT/LLM/TTS (or realtime speech services), and tool calls.

## Inbound and outbound call flow

- **Outbound:** validate an OIDC access token and `calls:write` scope, take the tenant ID only from the verified token, check voice subscription/channel entitlement, then ask the selected provider adapter to place the call.
- **Inbound:** verify the provider's webhook signature, resolve its configured account/number to a tenant, check that tenant's voice subscription, then accept or reject the call with a provider-specific response.
- Pass the same `AcceptedSession` shape and Pipecat pipeline builder to either direction. Provider adapters own dialing, answer/reject, hangup, transfer, and stream setup.
- Subscription checks reserve concurrency/usage capacity atomically before provider call control. The store must make reservations idempotent by session ID, release reservations on call end/failure, and update entitlements from verified subscription lifecycle events.
- Store `billing_mode` on the tenant account and select `prepaid` or `postpaid` during account creation; calls inherit that mode from the account entitlement. Prepaid admission checks and reserves available balance before answering/dialing, then settles actual usage and returns unused reservation. Postpaid admission checks account status and credit limit, then accrues finalized usage for invoicing. Make reservation and settlement idempotent by session ID and reconcile estimates against carrier call-detail records where available. Allow future mode changes through a deliberate, audited billing migration that handles existing reservations, balances, and outstanding invoices.
- Provision tenant accounts through an authenticated account API. The tenant identifier comes from verified identity claims; account creation accepts the display name and billing mode, never a caller-supplied tenant ID. Account provisioning and voice subscription activation remain separate operations.
- API routes fail closed if auth, the subscription authorizer, or a provider adapter is missing. The provider adapter must also verify caller ID and bot configuration ownership for outbound requests.

OIDC issuer, audience, JWKS URL, and tenant claim are environment configuration. Tokens are signature-checked against rotating JWKS keys and validated for issuer, audience, expiry, issue time, subject, tenant, and required scope. Keep identity and billing behind adapters so their vendors can be selected without changing voice call policy.

## Recording and observability

- Put Pipecat's `AudioBufferProcessor` in the pipeline and persist its audio callback through a `RecordingStore`. Keep recording disabled by default; require both the deployment setting and a per-session recording decision before starting it.
- Keep recording storage replaceable. Local WAV segments are for development; production storage needs tenant isolation, encryption, access controls, retention, and deletion handling.
- Emit structured JSON logs with `session_id`, channel, provider, and event fields. Never log audio, credentials, transcripts, or raw provider payloads by default.
- Expose request and session counters plus latency histograms through Prometheus. Create OpenTelemetry request spans and export over OTLP when configured. Add Pipecat pipeline/service metrics and provider-level media quality metrics as each channel is implemented.

## Adapter contract

Define a small internal interface for all channels. It should expose operations and events rather than leaking vendor request objects into the bot:

```python
class ChannelAdapter(Protocol):
    async def parse_inbound(self, request: Request) -> "InboundCall": ...
    async def accept_inbound(self, call: "InboundCall", session: "AcceptedSession") -> Response: ...
    async def initiate_outbound(self, call: "OutboundCall", session: "AcceptedSession") -> "AcceptedSession": ...
    async def build_transport(self, session: "AcceptedSession", connection: object) -> BaseTransport: ...
    async def hangup(self, session: "AcceptedSession", reason: str | None = None) -> None: ...
    async def transfer(self, session: "AcceptedSession", destination: str) -> None: ...
```

`AcceptedSession` should carry a stable internal `session_id`, `channel`, `provider`, `direction`, caller/callee identifiers, provider call ID, tenant/bot configuration IDs, and a provider-neutral `control` handle. Keep secrets and raw provider payloads out of the agent context; retain raw payloads only in appropriately protected diagnostics if needed.

Not every provider supports every operation. Make optional capabilities explicit (for example `supports_transfer`, `supports_dtmf`, `supports_outbound`) and return a typed unsupported-operation result instead of silently pretending success.

## Channel implementations

| Channel | Media integration | Adapter responsibility |
| --- | --- | --- |
| Browser or mobile voice | Pipecat WebRTC transport (SmallWebRTC for direct connections, or Daily/LiveKit when managed rooms are needed) | Session creation, auth, client signaling, disconnect lifecycle |
| Vobiz PSTN (first implementation) | `FastAPIWebsocketTransport` with Vobiz's `pipecat-vobiz` serializer | Vobiz Voice API calls, VobizXML answer flow, signed media callbacks, recording policy |
| Twilio, Piopiy, TeleCMI | WebSocket transport plus a provider serializer, or a focused custom Pipecat `FrameSerializer` when needed | Provider webhook/control API and protocol-specific audio/codec translation |
| WhatsApp voice | Pipecat `WhatsAppClient` and its WebRTC connection flow | Meta webhook verification, number/token configuration, WhatsApp call lifecycle |

Vobiz publishes `pipecat-vobiz`, so use that serializer and keep its Voice API and webhook handling in the Vobiz adapter. For Piopiy and TeleCMI, confirm the exact media-stream protocol, codecs, sample rates, event format, and call-control API against the selected account/API version before implementing adapters.

Provider credentials, provider settings, and phone-number capabilities use a shared tenant-scoped integration store. Credentials are encrypted with a deployment-managed Fernet key. Inbound number ownership is unique per provider so callback routing cannot select an ambiguous account; outbound caller IDs must be enabled on that tenant's integration. The Vobiz adapter is the first consumer; other providers can reuse the management API and storage while supplying their own validation and call-control logic.

## Pipeline composition

Use one pipeline builder that takes a normalized session and channel capabilities. Keep the conversation logic channel-neutral:

```text
transport input
  -> audio cleanup / VAD / turn strategy
  -> STT or realtime speech service
  -> conversation context + tools
  -> LLM or realtime speech service
  -> TTS (if applicable)
  -> transport output
```

Channel differences belong at the transport and call-control boundaries. The bot should receive normalized events such as `session_started`, `caller_speech_started`, `dtmf_received`, `session_ended`, and `handoff_requested`, not raw Twilio/Meta JSON.

Realtime model vendors also sit behind a separate `RealtimeAgentProvider` interface that creates Pipecat conversation processors for a normalized session. Vertex Gemini Live is the initial backend; later providers such as OpenAI Realtime can be registered without changing telephony adapters, recording, or session control. These active-call agents live under `agents/call/`. Post-call work has a separate `PostCallAgent` contract under `agents/post_call/`, uses LangChain for model abstraction and structured output, and receives normalized completed-call artifacts after the media session ends. Its initial model is Gemini on Vertex AI. Transcript collection feeds an encrypted PostgreSQL job outbox and worker. Tenant-configured callbacks deliver completed structured results with HMAC signatures and at-least-once retries; polling remains available as a fallback.

## Package boundaries

```text
src/chiefvoice/
  app.py                       # composition root: startup, wiring, router inclusion
  core/
    config.py                  # environment-backed application settings
  identity/
    auth.py                    # OIDC verification and scoped principals
  accounts/                    # tenant account creation, persistence, API
  billing/
    models.py                  # billing mode and entitlement types
    subscriptions.py           # account-mode selection and call authorization
    policies/
      prepaid.py               # prepaid ledger and policy boundary
      postpaid.py              # postpaid ledger and policy boundary
  calls/
    coordinator.py             # inbound/outbound call lifecycle and subscription checks
    routes.py                  # call API
  sessions/
    models.py                  # provider-neutral session and direction
  agents/
    call/                      # active-call Pipecat pipeline and realtime providers
      providers/               # Vertex now; OpenAI Realtime or others later
    post_call/                 # transcript capture, LangChain analysis, result outbox, webhooks
      queue.py                 # encrypted analysis jobs and signed callback delivery
      webhook_routes.py        # tenant callback configuration API
  recordings/                  # recording policy and storage adapters
  observability/               # structured logs, metrics, and request tracing
  data_lifecycle/
    retention.py               # tenant-scoped retention rules and purge targets
    backup.py                  # backup policy, artifact, verify/restore provider contract
  channels/
    base.py                    # adapter and capability contracts
    integrations.py            # provider-neutral credentials/numbers contract
    integration_store.py       # encrypted account integration persistence
    integration_routes.py      # tenant integration management API
    webrtc/                    # browser/mobile signaling
    twilio/                    # webhooks, call control, serializer setup
    vobiz/                     # provider integration and serializer
    piopiy/                    # provider integration and serializer
    telecmi/                   # provider integration and serializer
    whatsapp/                  # Meta webhook and Pipecat WhatsApp client
```

Keep `app.py` as the only composition root. Domain packages own their models, interfaces, persistence adapters, and routes; call orchestration depends on those interfaces rather than vendor-specific implementations.

## Build sequence

1. Configure an OIDC provider and subscription entitlement store; they are enforced before call control.
2. Wire the Pipecat STT/LLM/TTS processors using the shared pipeline factory.
3. Connect Vobiz credentials and number routing; exercise inbound and outbound through its official serializer.
4. Add direct WebRTC for browser/mobile voice.
5. Add Twilio, Piopiy, and TeleCMI after validating each provider's media and control protocol.
6. Add WhatsApp voice through its dedicated adapter; add WhatsApp messaging separately if needed, since chat messaging is not the same media path as a voice call.

This keeps the agent reusable without pretending different carriers have identical media or call-control behavior.
