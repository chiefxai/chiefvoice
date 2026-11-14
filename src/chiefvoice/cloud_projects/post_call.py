from chiefvoice.agents.post_call.base import CompletedCall, PostCallAgent, PostCallResult
from chiefvoice.agents.post_call.providers.vertex import create_vertex_post_call_agent
from chiefvoice.cloud_projects.models import TenantVertexProjectDirectory


class TenantVertexPostCallAgent:
    """Resolve the tenant's isolated Vertex project for each async analysis job."""

    name = "langchain_vertex_tenant_project"

    def __init__(
        self,
        directory: TenantVertexProjectDirectory,
        *,
        location: str,
        model: str,
        credentials_json: str | None = None,
        credentials_path: str | None = None,
    ):
        self.directory = directory
        self.location = location
        self.model = model
        self.credentials_json = credentials_json
        self.credentials_path = credentials_path

    async def process(self, call: CompletedCall) -> PostCallResult:
        project_id = await self.directory.get_ready_project_id(call.tenant_id)
        if project_id is None:
            raise RuntimeError("Tenant Vertex project is not ready for post-call processing")
        agent: PostCallAgent = create_vertex_post_call_agent(
            project_id=project_id,
            location=self.location,
            model=self.model,
            credentials_json=self.credentials_json,
            credentials_path=self.credentials_path,
        )
        return await agent.process(call)
