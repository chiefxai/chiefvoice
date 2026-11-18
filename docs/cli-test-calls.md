# CLI guide: create templates and trigger a test call

The `scripts/chiefvoice_test_call.py` command creates one API-scoped calling template and one API-scoped post-call template, then starts a real outbound phone call using those new template IDs. It is a single-command smoke test for the template-to-call flow.

The created templates are **not set as the API defaults**. They stay assigned to the API so you can inspect or reuse them. Each invocation creates a new pair; the CLI does not delete earlier templates.

## Requirements

- Run from the repository root after installing the project dependencies with `uv sync`.
- The API must be configured with an active outbound-enabled phone number assigned to the selected API.
- The API must have `calls:write` scope, a valid voice configuration, and active billing/voice entitlement.
- To create API-scoped templates, the platform admin token must identify an allowlisted admin and include `platform:customers:write`.
- For post-call results, post-call processing must be configured for the API. A recording is optional and requires platform/API recording policy to permit it.

The command uses two distinct credentials:

| Environment variable | Used for | Required access |
|---|---|---|
| `CHIEFVOICE_URL` | API server, e.g. `https://chiefvoice.elvoryx.in` | Reachable API base URL, without `/v1` |
| `CHIEFVOICE_API_KEY_ID` | Assigning created templates to the API | API ID from the platform console, not the secret key |
| `CHIEFVOICE_ADMIN_TOKEN` | Creating templates | Current OIDC **access token** for an allowlisted platform administrator with `platform:customers:write` |
| `CHIEFVOICE_API_KEY` | Starting the outbound call | Full customer API key with `calls:write` |

The admin token is not the OIDC ID token, password, or customer API key. In the signed-in console, the OIDC user session is held in browser session storage; its `access_token` can be used for the CLI if your organization permits copying it. Keep it private, don’t paste it into chat or commit it, and replace it after its normal expiry. The customer API key is shown once when created; retrieve it from your secure password manager/secret store.

## Set up credentials

Export the values in your terminal. Don’t put secrets directly into a command line, shell script committed to Git, or shared logs.

```bash
export CHIEFVOICE_URL="https://chiefvoice.elvoryx.in"
export CHIEFVOICE_API_KEY_ID="<API ID from the API page>"
export CHIEFVOICE_ADMIN_TOKEN="<platform admin OIDC access token>"
export CHIEFVOICE_API_KEY="<full customer API key>"
```

To clear them from the current shell when finished:

```bash
unset CHIEFVOICE_ADMIN_TOKEN CHIEFVOICE_API_KEY CHIEFVOICE_API_KEY_ID CHIEFVOICE_URL
```

## Basic call: create both templates and dial

This example asks for a name, preferred time, and address. The CLI asks you to confirm before creating templates and placing the real call.

```bash
uv run python scripts/chiefvoice_test_call.py \
  --to +916384670687 \
  --from +91YOUR_ASSIGNED_NUMBER \
  --language ta-IN \
  --live-instructions "Speak in Tamil using Chennai slang. Ask for the person's name, preferred time, and address one at a time. If they decline or do not know an answer, continue. Thank them and end the call when finished." \
  --post-call-instructions "Read the transcript and extract the name, preferred time, and address. Use empty strings for details not stated. Do not guess. Return a short factual summary." \
  --output-schema '{"type":"object","properties":{"name":{"type":"string"},"preferred_time":{"type":"string"},"address":{"type":"string"},"summary":{"type":"string"}},"required":["name","preferred_time","address","summary"],"additionalProperties":false}'
```

Omit `--from` only when the API has exactly one assigned outbound sender number. Provider is resolved automatically from that assigned number; the request does not take a provider field.

## Verify expected name and age after the call

Keep expected values in **post-call** context if the live agent should not see them. The instruction placeholders are declared in `--post-call-variables`, and their values are passed in `--post-call-context`. The post-call agent reads the transcript and compares it with those expected values.

```bash
uv run python scripts/chiefvoice_test_call.py \
  --to +916384670687 \
  --language ta-IN \
  --live-instructions "Speak in Tamil using Chennai slang. Ask the caller for their name and age, one at a time. Thank them and end the call." \
  --post-call-instructions "Compare the transcript with expected name {{expected_name}} and age {{expected_age}}. Use only what the caller actually said as evidence. Set each match field to true only when the stated value matches. Do not guess. Return the reported values and a short factual summary." \
  --post-call-variables '{"expected_name":{"type":"string","required":true},"expected_age":{"type":"integer","required":true}}' \
  --post-call-context '{"expected_name":"Avery","expected_age":32}' \
  --output-schema '{"type":"object","properties":{"name_matches":{"type":"boolean"},"age_matches":{"type":"boolean"},"reported_name":{"type":"string"},"reported_age":{"type":"string"},"summary":{"type":"string"}},"required":["name_matches","age_matches","reported_name","reported_age","summary"],"additionalProperties":false}'
```

For placeholders, every `{{variable_name}}` in instructions must have a matching declaration. Supported declaration types are `string`, `integer`, `number`, `boolean`, `object`, and `array`; `required: true` makes a corresponding context value mandatory. For example, `{{expected_age}}` requires both an `expected_age` declaration and a value in `--post-call-context`.

You can pass live-only expected/reference data using `--live-context` and `--live-variables`; this makes that data available to the live agent. Avoid placing information in live context when the caller should provide it without being prompted with the answer. Use `--include-live-context` only when the post-call agent also needs the live context.

## Options

| Option | Default | Purpose |
|---|---|---|
| `--to` | Required | Recipient number in E.164 format |
| `--from` | None | Sender number assigned to this API; required if multiple numbers are assigned |
| `--live-instructions` | Ask for name, preferred time, address | Instructions saved in the new calling template |
| `--post-call-instructions` | Extract name, preferred time, address, summary | Instructions saved in the new post-call template |
| `--live-context` | `{}` | JSON object passed only to the live agent |
| `--post-call-context` | `{}` | JSON object passed to post-call processing and used for placeholders |
| `--live-variables` | `{}` | JSON variable declarations for calling instruction placeholders |
| `--post-call-variables` | `{}` | JSON variable declarations for post-call instruction placeholders |
| `--output-schema` | name/time/address/summary object | Caller-defined JSON Schema for post-call output |
| `--language` | `en-US` | Language tag stored in calling-template settings, e.g. `ta-IN` |
| `--post-call-tools` | none | Comma-separated `calculator` and/or `time` |
| `--time-zone` | `Asia/Kolkata` | IANA zone used by the post-call time tool |
| `--time-format` | `%Y-%m-%dT%H:%M:%S%z` | `strftime` format; must include `%z` |
| `--recording` | Off | Request recording; API/platform policy must allow it |
| `--include-live-context` | Off | Also pass live context to the post-call agent |
| `--timeout` | 90 seconds | HTTP request timeout |
| `--yes` | Off | Skip interactive confirmation; required for non-interactive use |

The calling template includes the `end_call` tool and enables automatic hangup on completion. Post-call tools are limited to `calculator` and `time`. The channel is currently fixed to `phone`; WhatsApp and other channels are not supported by this CLI yet.

## What happens and what it prints

1. The CLI validates required environment variables and JSON options locally.
2. It asks for confirmation. Declining creates no templates and places no call.
3. It creates a calling template and a post-call template through the platform-admin API, assigned to `CHIEFVOICE_API_KEY_ID`.
4. It sends `POST /v1/calls/outbound` using the customer API key and both newly returned template IDs.
5. It prints the template IDs and the call response, including `session_id` and initial status.

Call creation returns an accepted session; it does not wait for the phone conversation or post-call extraction to finish. The templates are retained even if call creation later fails, so inspect the API page before rerunning if a request fails after template creation.

## Check status and result

Use the session ID printed by the CLI:

```bash
curl "$CHIEFVOICE_URL/v1/calls/$SESSION_ID" \
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY"

curl "$CHIEFVOICE_URL/v1/calls/$SESSION_ID/post-call" \
  -H "Authorization: Bearer $CHIEFVOICE_API_KEY"
```

The first endpoint reports call status. The second reports queued/running/completed/failed post-call status and the structured result. Use `/docs` on your API host for the live Swagger reference and exact schemas.

## Safety and troubleshooting

- A test call reaches a real phone number and may incur telephony, model, and recording costs. The confirmation prompt is enabled by default.
- `401` usually means an expired/wrong token or API key. Refresh the OIDC access token; verify the API key is copied in full.
- `403` can mean the OIDC subject is not a platform admin, the token lacks `platform:customers:write`, the customer key lacks `calls:write`, or the selected number/template is not assigned to that API.
- `422` means a request field, context variable, time zone/format, tool, or output schema failed validation. Correct the returned field detail before retrying.
- A network timeout during call creation is **not retried** because the provider may already have accepted the call. Check call history and logs using the call ID/template IDs before running it again.
- A successful call-start response does not mean post-call analysis is complete. Poll the post-call endpoint; webhook delivery can also be used if configured.

See `uv run python scripts/chiefvoice_test_call.py --help` for the CLI's built-in usage text.
