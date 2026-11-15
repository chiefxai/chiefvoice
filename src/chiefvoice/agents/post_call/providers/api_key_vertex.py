"""Select the post-call Vertex project and credentials belonging to a calling API key."""

from chiefvoice.agents.post_call.base import CompletedCall, PostCallAgent, PostCallResult
from chiefvoice.agents.post_call.providers.vertex import create_vertex_post_call_agent
from chiefvoice.identity.api_keys import ApiKeyDirectory


class ApiKeyVertexPostCallAgent:
    name = "api_key_vertex"

    def __init__(self, directory: ApiKeyDirectory, *, model: str, fallback: PostCallAgent | None = None):
        self.directory = directory
        self.model = model
        self.fallback = fallback

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
        agent = create_vertex_post_call_agent(
            project_id=config.post_call_project_id or config.project_id,
            location=config.post_call_location or config.location,
            model=config.post_call_model,
            credentials_json=config.post_call_credentials_json or config.credentials_json,
        )
        return await agent.process(call)
