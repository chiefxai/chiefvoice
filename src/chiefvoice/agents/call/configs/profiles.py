from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class SpeakingStyle(StrEnum):
    PROFESSIONAL = "professional"
    FRIENDLY = "friendly"
    CASUAL = "casual"
    SLANG = "slang"


@dataclass(frozen=True, slots=True)
class AgentProfile:
    agent_id: str
    tenant_id: str
    name: str
    language: str
    voice: str
    speaking_style: SpeakingStyle
    instructions: str
    enabled: bool
    created_at: datetime
    updated_at: datetime

    def system_instruction(self, base_instruction: str) -> str:
        style_guidance = {
            SpeakingStyle.PROFESSIONAL: "Speak clearly and professionally.",
            SpeakingStyle.FRIENDLY: "Use a warm, friendly, and helpful speaking style.",
            SpeakingStyle.CASUAL: "Use a relaxed, conversational speaking style.",
            SpeakingStyle.SLANG: (
                "Use natural, mild slang appropriate to the configured language and audience. "
                "Avoid profanity and do not force slang into every sentence."
            ),
        }[self.speaking_style]
        parts = [base_instruction.strip(), f"Respond in {self.language}.", style_guidance]
        if self.instructions.strip():
            parts.append(f"Agent instructions:\n{self.instructions.strip()}")
        return "\n\n".join(part for part in parts if part)


class AgentProfileDirectory(Protocol):
    async def initialize(self) -> None: ...

    async def close(self) -> None: ...

    async def create(self, profile: AgentProfile) -> AgentProfile: ...

    async def get(self, tenant_id: str, agent_id: str) -> AgentProfile | None: ...

    async def list_for_tenant(self, tenant_id: str) -> list[AgentProfile]: ...

    async def update(self, profile: AgentProfile) -> AgentProfile | None: ...

    async def archive(self, tenant_id: str, agent_id: str) -> bool: ...
