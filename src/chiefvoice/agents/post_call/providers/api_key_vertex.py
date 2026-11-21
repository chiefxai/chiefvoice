"""Select the post-call Vertex project and credentials belonging to a calling API key."""

import hashlib
import logging

from chiefvoice.agents.post_call.base import CompletedCall, PostCallAgent, PostCallResult
from chiefvoice.agents.post_call.providers.vertex import create_vertex_post_call_agent
from chiefvoice.identity.api_keys import ApiKeyDirectory

logger = logging.getLogger(__name__)


class ApiKeyVertexPostCallAgent:
    name = "api_key_vertex"

    def __init__(self, directory: ApiKeyDirectory, *, model: str, fallback: PostCallAgent | None = None):
        self.directory = directory
        self.model = model
        self.fallback = fallback
        self._agents_by_result_id: dict[int, PostCallAgent] = {}

    def compatibility_fields(self, result: PostCallResult) -> dict:
        """Forward registered workflow compatibility projections."""
        producer = self._agents_by_result_id.get(id(result), self.fallback)
        projector = getattr(producer, "compatibility_fields", None)
        if callable(projector):
            return projector(result)
        return {}

    def legacy_compatibility_fields(
        self, result: PostCallResult, legacy_payload: dict
    ) -> dict:
        producer = self._agents_by_result_id.pop(id(result), self.fallback)
        projector = getattr(producer, "legacy_compatibility_fields", None)
        if callable(projector):
            return projector(result, legacy_payload)
        return {}

    async def process(self, call: CompletedCall) -> PostCallResult:
        if call.api_key_id is None:
            if self.fallback is None:
                raise RuntimeError("No API key is associated with this call")
            return await self.fallback.process(call)
        config = await self.directory.get_vertex_config(call.api_key_id)
        if config is None:
            raise RuntimeError("GCP credentials are not configured for this API key")
        if config.post_call_provider != "vertex":
            raise RuntimeError("The selected post-call provider is not available")
        project_id = config.post_call_project_id or config.project_id
        location = config.post_call_location or config.location
        model = config.post_call_model
        try:
            agent = create_vertex_post_call_agent(
                project_id=project_id,
                location=location,
                model=model,
                credentials_json=config.post_call_credentials_json or config.credentials_json,
            )
            result = await agent.process(call)
            self._agents_by_result_id[id(result)] = agent
            return result
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status is None:
                status = getattr(exc, "code", None)
            if callable(status):
                status = None
            logger.error(
                "post_call_vertex_request_failed",
                extra={
                    "session_id": call.session_id,
                    "tenant_id": call.tenant_id,
                    "model": model,
                    "location": location,
                    "project_fingerprint": hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16],
                    "error_type": type(exc).__name__,
                    "error_status": status if isinstance(status, (str, int)) else None,
                },
            )
            raise
