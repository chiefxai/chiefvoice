import asyncio
import logging

import functions_framework
from flask import jsonify

from chiefvoice.agents.post_call.lambda_handler import _process

logger = logging.getLogger(__name__)


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
    try:
        result = asyncio.run(_process(event))
    except Exception:
        logger.exception("post_call_function_failed")
        return jsonify({"error": "post_call_processing_failed"}), 500
    return jsonify(result), 200
