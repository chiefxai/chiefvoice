import asyncio
import json

import functions_framework
from flask import jsonify

from chiefvoice.agents.post_call.lambda_handler import _process

def _log(severity: str, event_name: str, **fields: object) -> None:
    """Emit structured Cloud Logging entries without call content or payload fields."""
    print(
        json.dumps(
            {"severity": severity, "event": event_name, **fields},
            separators=(",", ":"),
        ),
        flush=True,
    )


@functions_framework.http
def handler(request):
    if request.method != "POST":
        return jsonify({"error": "method_not_allowed"}), 405
    event = request.get_json(silent=True)
    if (
        not isinstance(event, dict)
        or event.get("schema_version") != 1
        or not isinstance(event.get("job_id"), str)
        or not event["job_id"]
    ):
        return jsonify({"error": "invalid_event"}), 400
    _log("INFO", "post_call_function_started")
    try:
        result = asyncio.run(_process(event))
    except Exception as exc:
        _log("ERROR", "post_call_function_failed", error_type=type(exc).__name__)
        return jsonify({"error": "post_call_processing_failed"}), 500
    _log("INFO", "post_call_function_finished", status=result["status"])
    return jsonify(result), 200
