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

    def system_instruction(
        self, base_instruction: str, *, language: str | None = None
    ) -> str:
        effective_language = language or self.language
        language_guidance = {
            "ta": "Respond unmistakably in Tamil (தமிழ்). Prefer natural spoken Tamil over literary Tamil.",
            "ml": "Respond unmistakably in Malayalam (മലയാളം). Prefer natural spoken Malayalam over formal written Malayalam.",
            "te": "Respond unmistakably in Telugu (తెలుగు). Prefer natural spoken Telugu over formal written Telugu.",
        }
        language_key = effective_language.split("-", 1)[0].lower()
        style_guidance = {
            SpeakingStyle.PROFESSIONAL: "Speak clearly and professionally.",
            SpeakingStyle.FRIENDLY: "Use a warm, friendly, and helpful speaking style.",
            SpeakingStyle.CASUAL: "Use a relaxed, conversational speaking style.",
            SpeakingStyle.SLANG: {
                "ta": "Use mild, familiar colloquial Tamil naturally and sparingly; for example, ‘செம்ம’ (great/cool) when it fits the relationship and context. Use respectful address when appropriate. Avoid profanity, caste or community-coded expressions, and caricatured regional dialects; do not force English into Tamil.",
                "ml": "Use mild, familiar colloquial Malayalam naturally and sparingly; for example, ‘പൊളി’ (great/cool) when it fits the relationship and context. Use respectful address when appropriate. Avoid profanity, caste or community-coded expressions, and caricatured regional dialects; do not force English into Malayalam.",
                "te": "Use mild, familiar colloquial Telugu naturally and sparingly; for example, ‘మస్తు’ (great/cool) when it fits the relationship and context. Use respectful address when appropriate. Avoid profanity, caste or community-coded expressions, and caricatured regional dialects; do not force English into Telugu.",
                "default": "Use natural, mild slang appropriate to the configured language and audience. Avoid profanity and do not force slang into every sentence.",
            }.get(language_key, "Use natural, mild slang appropriate to the configured language and audience. Avoid profanity and do not force slang into every sentence."),
        }[self.speaking_style]
        parts = [
            base_instruction.strip(),
            f"Respond in {effective_language}.",
            language_guidance.get(language_key, ""),
            style_guidance,
        ]
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
