#!/usr/bin/env python3
"""Create API-scoped test templates and place one real outbound test call."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Any

import httpx


DEFAULT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "preferred_time": {"type": "string"},
        "address": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["name", "preferred_time", "address", "summary"],
    "additionalProperties": False,
}


def json_object(value: str, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def json_value(value: str, label: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc}") from exc


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Set the {name} environment variable")
    return value


def confirm_call(skip_confirmation: bool) -> None:
    if skip_confirmation:
        return
    if not sys.stdin.isatty():
        raise ValueError("Use --yes to confirm a real outbound call in a non-interactive shell")
    answer = input("This creates two API templates and places a real call that may incur charges. Continue? [y/N] ")
    if answer.strip().lower() not in {"y", "yes"}:
        raise ValueError("Cancelled; no templates were created and no call was placed")


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.request(
        method,
        path,
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:1000]
        raise RuntimeError(
            f"{method} {path} returned HTTP {response.status_code}: "
            f"{json.dumps(detail, ensure_ascii=False)}"
        ) from exc
    if response.status_code == 204 or not response.content:
        return {}
    try:
        parsed = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{method} {path} returned a non-JSON response") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{method} {path} returned an unexpected response")
    return parsed


def create_template(
    client: httpx.Client,
    *,
    api_key_id: str,
    admin_token: str,
    kind: str,
    name: str,
    instructions: str,
    variables: dict[str, Any],
    tools: list[dict[str, str]],
    settings: dict[str, Any],
) -> dict[str, Any]:
    return request_json(
        client,
        "POST",
        f"/v1/admin/customers/api-keys/{api_key_id}/agent-templates",
        token=admin_token,
        body={
            "name": name,
            "kind": kind,
            "instructions": instructions,
            "variables": variables,
            "tools": tools,
            "settings": settings,
            "is_default": False,
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create calling and post-call templates assigned to one API, then start a real "
            "outbound test call."
        ),
        epilog=(
            "Required environment: CHIEFVOICE_URL, CHIEFVOICE_API_KEY_ID, "
            "CHIEFVOICE_ADMIN_TOKEN, CHIEFVOICE_API_KEY. The admin token needs "
            "platform:customers:write; the API key needs calls:write."
        ),
    )
    parser.add_argument("--to", required=True, dest="destination", help="Recipient phone number in E.164 format")
    parser.add_argument("--from", dest="caller_id", help="Assigned sender number; omit if this API has only one")
    parser.add_argument(
        "--live-instructions",
        default=(
            "You are a concise phone assistant. Ask the person for their name, preferred time, "
            "and address, one at a time. If they do not know or do not want to provide a value, "
            "accept that and continue. When finished, thank them and end the call."
        ),
        help="Instructions saved in the calling template",
    )
    parser.add_argument(
        "--post-call-instructions",
        default=(
            "Read the transcript and extract the person's name, preferred time, and address. "
            "Use empty strings for details not stated. Do not infer missing facts. Provide a short factual summary."
        ),
        help="Instructions saved in the post-call template",
    )
    parser.add_argument("--live-context", default="{}", help="Live agent context as a JSON object")
    parser.add_argument("--post-call-context", default="{}", help="Post-call context as a JSON object")
    parser.add_argument(
        "--live-variables",
        default="{}",
        help="Calling template variable declarations as a JSON object",
    )
    parser.add_argument(
        "--post-call-variables",
        default="{}",
        help="Post-call template variable declarations as a JSON object",
    )
    parser.add_argument("--output-schema", help="Post-call JSON Schema; defaults to name/time/address/summary")
    parser.add_argument("--time-zone", default="Asia/Kolkata", help="Post-call time tool timezone (IANA name)")
    parser.add_argument(
        "--time-format",
        default="%Y-%m-%dT%H:%M:%S%z",
        help="Post-call time format; must include %%z for the UTC offset",
    )
    parser.add_argument(
        "--post-call-tools",
        default="",
        help="Comma-separated post-call tools: calculator,time",
    )
    parser.add_argument("--recording", action="store_true", help="Request recording if API policy allows it")
    parser.add_argument("--include-live-context", action="store_true", help="Give post-call agent the live context too")
    parser.add_argument("--language", default="en-US", help="Calling template language, e.g. ta-IN")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation before creating templates and dialing")
    parser.add_argument("--timeout", type=float, default=90, help="HTTP request timeout in seconds (default: 90)")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        base_url = required_env("CHIEFVOICE_URL").rstrip("/")
        api_key_id = required_env("CHIEFVOICE_API_KEY_ID")
        admin_token = required_env("CHIEFVOICE_ADMIN_TOKEN")
        call_api_key = required_env("CHIEFVOICE_API_KEY")
        live_context = json_object(args.live_context, "--live-context")
        post_context = json_object(args.post_call_context, "--post-call-context")
        live_variables = json_object(args.live_variables, "--live-variables")
        post_variables = json_object(args.post_call_variables, "--post-call-variables")
        schema = json_value(args.output_schema, "--output-schema") if args.output_schema else DEFAULT_OUTPUT_SCHEMA
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("--output-schema must be a JSON Schema object with type 'object'")
        requested_tools = [name.strip() for name in args.post_call_tools.split(",") if name.strip()]
        unsupported = set(requested_tools) - {"calculator", "time"}
        if unsupported:
            raise ValueError("Unsupported post-call tools: " + ", ".join(sorted(unsupported)))
        if args.timeout <= 0:
            raise ValueError("--timeout must be greater than zero")

        confirm_call(args.yes)

        suffix = uuid.uuid4().hex[:8]
        with httpx.Client(base_url=base_url, timeout=httpx.Timeout(args.timeout, connect=10)) as client:
            calling = create_template(
                client,
                api_key_id=api_key_id,
                admin_token=admin_token,
                kind="calling",
                name=f"CLI test calling {suffix}",
                instructions=args.live_instructions,
                variables=live_variables,
                tools=[{"type": "builtin", "name": "end_call"}],
                settings={"language": args.language, "auto_hangup_when_complete": True},
            )
            post_call = create_template(
                client,
                api_key_id=api_key_id,
                admin_token=admin_token,
                kind="post_call",
                name=f"CLI test post-call {suffix}",
                instructions=args.post_call_instructions,
                variables=post_variables,
                tools=[{"type": "builtin", "name": name} for name in requested_tools],
                settings={
                    "time_zone": args.time_zone,
                    "time_format": args.time_format,
                    "output_schema": schema,
                },
            )
            print(f"Created calling template: {calling.get('id')}")
            print(f"Created post-call template: {post_call.get('id')}")

            call_request: dict[str, Any] = {
                "channel": "phone",
                "destination": args.destination,
                "recording_allowed": args.recording,
                "live_agent": {"template_id": calling["id"], "context": live_context},
                "post_call": {
                    "template_id": post_call["id"],
                    "context": post_context,
                    "output_schema": schema,
                    "include_live_agent_context": args.include_live_context,
                },
            }
            if args.caller_id:
                call_request["caller_id"] = args.caller_id

            try:
                result = request_json(
                    client,
                    "POST",
                    "/v1/calls/outbound",
                    token=call_api_key,
                    body=call_request,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise RuntimeError(
                    "Call-start request had a network error. It was not retried because the provider "
                    "may already have accepted the call. Check call history before running the command again."
                ) from exc

        print("Call accepted:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("Templates remain assigned to the API for reuse; this command does not delete them.")
        return 0
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
