# Outbound call request and reusable agent templates

Status: implemented contract for outbound reusable templates and failure webhooks.

## Goal

Let a customer API start a call with a reusable live-call template and a reusable post-call template, while keeping per-call data and the requested result schema separate. Resolve the templates before dialing, validate the merged configuration, and preserve an immutable snapshot for that call and its later post-call job.

## Baseline checked before implementation

The public endpoint is `POST /v1/calls/outbound` in `src/chiefvoice/calls/routes.py`. The baseline accepted provider and inline prompt fields. This implementation removes those fields from the new contract: the authenticated API key and selected sender number identify the provider, and saved templates supply instructions.

The post-call tools currently supported are `calculator` and `time`. The realtime agent has an end-call capability controlled by its configured profile. Reusable platform-managed templates and per-API assignments are part of this implementation. The current simulation route reuses the outbound body and only supports the phone channel.

## Proposed request contract

The API key and selected sender resource determine the provider, so clients do not send `provider`. `channel` describes the communication medium (`phone`, later `whatsapp`). The platform resolves the provider by looking up `caller_id` in the authenticated API key's assigned sender numbers for that channel. The API key must also be allowed to use the selected template IDs.

```json
{
  "channel": "phone",
  "destination": "+15551234567",
  "caller_id": "+15557654321",
  "recording_allowed": true,
  "live_agent": {
    "template_id": "temp-live-xyz",
    "language": "ta-IN",
    "context": {
      "patient_name": "Avery"
    },
    "add_tools": [
      {"type": "builtin", "name": "end_call"}
    ]
  },
  "post_call": {
    "template_id": "temp-post-abc",
    "context": {
      "additional_notes": "Handle with care."
    },
    "add_tools": [
      {"type": "builtin", "name": "calculator"}
    ],
    "time_zone": "Asia/Kolkata",
    "time_format": "%Y-%m-%dT%H:%M:%S%z",
    "output_schema": {
      "type": "object",
      "properties": {"confirmed": {"type": "boolean"}},
      "required": ["confirmed"]
    },
    "include_live_agent_context": true
  }
}
```

Instructions are stored on the reusable template and are not sent in the call request. If `template_id` is omitted or null, the platform uses the default template of that kind assigned to the API key. If there is no configured default, reject before dialing with a clear error asking the administrator to assign one. The caller never needs to supply prompt instructions inline.

New requests do not accept `provider` or inline prompt/task instructions. Existing integrations must add template assignments/defaults to their API configuration and move instructions into those templates before using this contract. `live_agent.agent_id` and the top-level `bot_config_id` remain temporarily for selecting a voice profile; they are distinct from reusable prompt `template_id` and may be deprecated later.

## Reusable template resource

Create separate template records for `calling` and `post_call` kinds. A template contains:

- Stable ID, display name, kind, revision, and enabled/deleted state.
- Instructions, including explicit variables such as `{{patient_name}}`.
- Default built-in tool definitions and optional tool settings.
- Optional defaults appropriate to its kind: live voice/profile selection for calling; timezone, time format, and an optional default output schema for post-call.
- A variable declaration/schema so required placeholders and types can be checked before a call is placed.

Suggested admin endpoints:

- `POST /v1/admin/agent-templates` — create a template.
- `GET /v1/admin/agent-templates` and `GET /v1/admin/agent-templates/{template_id}` — list/view.
- `PATCH /v1/admin/agent-templates/{template_id}` — create a new revision/update editable fields.
- `DELETE /v1/admin/agent-templates/{template_id}` — disable it for new calls while retaining revisions referenced by existing calls.

Templates should be platform-admin managed in the first release, matching the current single-admin console. A customer API key may only use template IDs assigned to that API. Template CRUD and assignment must enforce this boundary, so knowing another template ID cannot grant access to it. If customer self-service accounts are added later, the template owner can become a customer tenant without changing call execution semantics.

## Context and prompt rendering

Both `live_agent.context` and `post_call.context` should be JSON objects. They remain separate. The object serves two purposes:

1. Values matching declared template variables replace their corresponding `{{variable}}` placeholders.
2. All context values, including keys that are not placeholders, are passed to that agent as structured per-call context. Extra keys are not discarded.

Only declared variables may be interpolated into template instructions. Missing required variables fail validation before dialing. Unknown variables remain structured context but do not modify instructions. Use a safe renderer (no Python, Jinja execution, expressions, or attribute access), bound prompt/context byte sizes, and treat all request context as untrusted data rather than trusted system instructions.

For a null `template_id`, resolve the API key's default template for that agent kind and validate its declared variables against the supplied context. If no default exists, fail before dialing and ask the administrator to assign one. Instructions remain in the saved template, never in the call request.

## Tool selection and override rules

Normalize tools as objects with a `type` and `name`; initially allow only `type: "builtin"`. Validate each tool against a channel/agent-kind allowlist. For the initial implementation, support `end_call` for live calls and `calculator`/`time` for post-call. Unknown tools or channel-incompatible tools fail before dialing/post-call execution.

No `tools_mode` is needed. Its purpose would have been to select how request tools combine with template defaults, which is easy to miss and adds a switch callers must understand. Use explicit behavior instead: if `tools` is omitted, inherit the template's tools; if `tools` is present, it replaces the template's tool list; an empty list disables optional tools. Add an `add_tools` array for the less common case of extending template tools. Reject a request that sends both `tools` and `add_tools`. This keeps replacement and addition clear without a mode flag or accepting arbitrary executable tool definitions. Future customer HTTP tools need a separately authenticated and permissioned tool registry.

## Channel and phone-number semantics

`channel` answers “what medium?”. The platform determines “which integration handles it?” from the authenticated API key's assigned number/resource. For a phone call, match `caller_id` against that API key's assigned phone numbers and resolve its provider (currently Vobiz). If exactly one eligible sender exists, `caller_id` may be omitted; otherwise require it. If the same sender identity is assigned through multiple provider configurations and resolution is ambiguous, reject rather than guessing. In the first implementation, `phone` is supported and `whatsapp` is rejected as unsupported until its adapter, sender-resource mapping, address rules, and callbacks are implemented.

For phone calls, `destination` is the recipient and `caller_id` is the selected sender number. The sender must be assigned to the authenticated API key. For a future WhatsApp channel, these values identify its recipient and configured WhatsApp sender resource, subject to channel-specific validation. Provider selection always comes from that sender resource.

`recording_allowed` is a per-call opt-in, still bounded by the API's recording setting and the platform-wide recording policy. A request cannot enable recording if the API or platform has recording disabled.

## Post-call behavior

The post-call task/instructions and its context are independent from live-call instructions/context. `include_live_agent_context` explicitly controls whether the post-call worker also receives the live context. When enabled, pass that exact structured object as a separately named input; do not merge it into post-call context or overwrite duplicate keys silently.

The request's `output_schema` is the schema the caller expects. If omitted, use a template default only if one exists; otherwise reject the request when post-call processing is enabled. Validate the schema before dialing, then validate the generated result with Pydantic before storing it as successful or sending the configured webhook. Invalid output follows the existing retry/failure path and is not delivered as a successful result.

`time_zone` and `time_format` remain explicit fields inside `post_call` in the request structure. A template may supply defaults; request values override them. The deterministic time tool uses these settings and returns a formatted value with an explicit UTC offset. Include both settings in the API's result metadata so the receiving backend knows how extracted/formatted times were interpreted, while keeping `result` itself exactly compliant with the caller's output schema. For example:

```json
{
  "event": "call.analysis.completed",
  "event_id": "post_call.analysis.completed:call-id",
  "call": {"session_id": "call-id", "channel": "phone"},
  "post_call": {
    "time_zone": "Asia/Kolkata",
    "time_format": "%Y-%m-%dT%H:%M:%S%z"
  },
  "result": {"confirmed": true}
}
```

## Failure webhook routing

Failure notifications are delivered without invoking post-call AI. Configure the API's normal Call outcome webhook and signing secret first. Each API-owned post-call template may optionally set `failure_webhook_url` and a static `failure_webhook_payload` object in its settings. This lets one API key serve multiple integrations while each post-call template routes failure notifications to the corresponding service.

For a call associated with a post-call template, that template's failure URL overrides the API-level failure URL; if it is not set, delivery uses the API-level failure URL, then the normal Call outcome URL. A template's custom payload replaces the API-level custom payload for that call. The custom object is added under `custom_payload`, so it cannot overwrite the standard call ID, event ID, or failure details. The API's webhook signing secret signs deliveries to both endpoints.

Failure events include `call.failed` and `call.post_call.failed`. Missing or empty transcript skips post-call AI and immediately queues `call.post_call.failed`. Exhausted post-call processing and expired worker leases also queue a failure event. Delivery is durable, retried, and idempotent by event ID; receivers should deduplicate using `X-Chiefvoice-Event-Id` or the body `event_id`. Template URL overrides are validated as public HTTPS destinations when saved. Static custom payloads are limited to 16 KiB and are never generated by AI.

The API-wide fallback can be configured during API creation, or updated with `PUT /v1/webhooks/post-call`:

```json
{
  "target_url": "https://default.example.com/chiefvoice",
  "failure_webhook_url": "https://default.example.com/chiefvoice/failures",
  "failure_webhook_payload": {"source": "chiefvoice"}
}
```

For template-specific settings, use the API Templates section and enter `failure_webhook_url` and `failure_webhook_payload` in a post-call template. A template override requires the API's Call outcome webhook configuration so its signing secret is available.

## Resolution and lifecycle

1. Authenticate the API key and validate its scopes, assigned sender number, template assignments, and channel. Resolve provider from the assigned sender resource; reject ambiguous mappings.
2. Load enabled template revisions and verify their kind matches the nested agent section.
3. Merge defaults, request overrides, context variables, and tool selections; validate the final config and output schema before dialing.
4. Create the call and persist an encrypted snapshot of the effective prompts, contexts, schema, tools, template IDs/revisions, and recording decision. Secrets and sensitive context must not be written to logs.
5. Run the live agent from that snapshot. On call completion, enqueue post-call work using the same immutable snapshot, subject to transcript-presence rules.
6. Validate the post-call result, persist it, then deliver the API's configured webhook or make it available through polling.

Editing or disabling a template affects new calls only. Existing and queued calls use their captured revision so an edit cannot change an in-progress conversation or its post-call result.

## Validation and failure behavior

- Unknown or disabled template: reject before dialing.
- Template kind does not match `live_agent` or `post_call`: reject before dialing.
- Required placeholder missing or invalid type: reject before dialing and identify the missing variable without echoing sensitive values.
- Invalid output schema, timezone, time format, tool name, or unsupported channel: reject before dialing.
- Per-call context/prompt exceeds configured size: reject before dialing.
- Post-call schema mismatch: mark extraction failed/retry per policy; do not send an invalid success webhook.
- Unsupported WhatsApp request today: return a clear `422 channel_not_supported` response.

## Migration plan

1. Add template storage, revisioning, CRUD, assignment/default-template mapping per API key, and access checks.
2. Add the new `template_id`, JSON-object context, structured tools, and safe rendering while keeping existing inline request fields working.
3. Update the simulation form to pick templates, preview resolved prompts and variables, edit per-call contexts, and show the computed tool set before sending.
4. Add call-detail visibility for template ID/revision and effective settings, redacting secrets and sensitive context by default.
5. Update SDK/OpenAPI docs and examples; mark legacy `agent_id`/`bot_config_id` behavior deprecated after clients are migrated.
6. Implement WhatsApp only as a later channel adapter with explicit provider capability and webhook semantics.

## Acceptance criteria

- A call can use two independent templates and two independent object contexts.
- Correct placeholder values render; missing declared values fail before provider dialing; extra context fields remain available as structured context.
- Omitted request tools inherit template tools; `tools` replaces them; `add_tools` extends them, with kind-specific allowlist checks.
- Provider is resolved from the API key's selected sender number; clients do not send a provider field.
- `time_zone` and `time_format` are present in the request's `post_call` object and in result metadata, while the caller's `result` schema remains unchanged.
- Template changes do not alter already-started calls or queued post-call work.
- The post-call result is checked against the caller's output schema before success storage/webhook delivery.
- `recording_allowed` cannot exceed API/platform recording policy.
- Phone calls work with currently assigned Vobiz numbers; WhatsApp is explicitly rejected until implemented.
- Existing clients continue to work during migration.
