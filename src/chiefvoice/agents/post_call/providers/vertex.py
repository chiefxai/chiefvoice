import json

from chiefvoice.agents.post_call.langchain_agent import LangChainPostCallAgent
from chiefvoice.agents.post_call.workflows.builtins import build_builtin_workflow_registry
from chiefvoice.agents.post_call.workflows import PostCallWorkflowRegistry


def create_vertex_post_call_agent(
    *,
    project_id: str,
    location: str,
    model: str,
    credentials_json: str | None = None,
    credentials_path: str | None = None,
    workflow_registry: PostCallWorkflowRegistry | None = None,
) -> LangChainPostCallAgent:
    """Build a LangChain Gemini chat model backed by Vertex AI and wrap it as an agent."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    credentials = None
    if credentials_json:
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_info(
            json.loads(credentials_json),
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    elif credentials_path:
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
    model_instance = ChatGoogleGenerativeAI(
        model=model,
        vertexai=True,
        project=project_id,
        location=location,
        credentials=credentials,
        temperature=0,
    )
    registry = workflow_registry or build_builtin_workflow_registry(model_instance)
    return LangChainPostCallAgent(model_instance, workflow_registry=registry)
