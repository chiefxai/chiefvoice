from collections.abc import Sequence

from fastapi import HTTPException
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transcriptions.language import Language

from chiefvoice.agents.call.configs.profiles import AgentProfileDirectory
from chiefvoice.agents.call.providers.base import RealtimeAgentProvider
from chiefvoice.sessions import AcceptedSession


class VertexGeminiLiveProvider(RealtimeAgentProvider):
    """Pipecat Gemini Live speech-to-speech service hosted on Vertex AI."""

    name = "vertex"

    def __init__(
        self,
        *,
        project_id: str,
        location: str,
        model: str,
        voice: str,
        system_instruction: str,
        profile_directory: AgentProfileDirectory | None = None,
        credentials_json: str | None = None,
        credentials_path: str | None = None,
    ):
        if not project_id or not location:
            raise ValueError("Vertex AI project ID and location are required")
        self.project_id = project_id
        self.location = location
        self.model = model
        self.voice = voice
        self.system_instruction = system_instruction
        self.profile_directory = profile_directory
        self.credentials_json = credentials_json
        self.credentials_path = credentials_path

    async def create_processors(self, session: AcceptedSession) -> Sequence[FrameProcessor]:
        # Import lazily so deployments using another backend do not require Google packages.
        from pipecat.services.google.gemini_live.vertex.llm import GeminiLiveVertexLLMService

        voice = self.voice
        system_instruction = self.system_instruction
        params = None
        if session.bot_config_id is not None:
            if self.profile_directory is None or session.tenant_id is None:
                raise HTTPException(
                    status_code=503, detail="Voice agent profile storage is not configured"
                )
            profile = await self.profile_directory.get(session.tenant_id, session.bot_config_id)
            if profile is None:
                raise HTTPException(status_code=404, detail="Voice agent profile not found")
            try:
                language = Language(profile.language)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail="The selected realtime provider does not support this language tag",
                ) from exc
            voice = profile.voice
            system_instruction = profile.system_instruction(self.system_instruction)
            params = GeminiLiveVertexLLMService.InputParams(language=language)

        service = GeminiLiveVertexLLMService(
            project_id=self.project_id,
            location=self.location,
            credentials=self.credentials_json,
            credentials_path=self.credentials_path,
            model=self.model,
            voice_id=voice,
            system_instruction=system_instruction,
            params=params,
        )
        return (service,)
