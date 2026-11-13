import pytest
from pydantic import ValidationError

from chiefvoice.agents.post_call.base import CompletedCall
from chiefvoice.agents.post_call.langchain_agent import LangChainPostCallAgent


class _FakeStructuredModel:
    def __init__(self, result):
        self.result = result

    async def ainvoke(self, _messages, config=None):
        return self.result


class _FakeChatModel:
    def __init__(self, result):
        self.result = result
        self.requested_schema = None

    def with_structured_output(self, schema):
        self.requested_schema = schema
        return _FakeStructuredModel(self.result)


def _call():
    return CompletedCall(
        session_id="session-1",
        tenant_id="tenant-1",
        provider="vobiz",
        channel="phone",
        direction="outbound",
        transcript="Caller: Yes, Tuesday works for me.\nAssistant: Confirmed.",
        post_call_context="Record whether the appointment was confirmed.",
        output_schema={
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "string",
                    "enum": ["confirmed", "unresolved"],
                }
            },
            "required": ["outcome"],
        },
    )


@pytest.mark.asyncio
async def test_post_call_agent_rejects_output_outside_customer_schema():
    chat_model = _FakeChatModel({"outcome": "invented"})
    agent = LangChainPostCallAgent(chat_model)

    with pytest.raises(ValidationError):
        await agent.process(_call())


@pytest.mark.asyncio
async def test_post_call_agent_returns_validated_custom_output():
    chat_model = _FakeChatModel({"outcome": "confirmed"})
    agent = LangChainPostCallAgent(chat_model)

    result = await agent.process(_call())

    assert chat_model.requested_schema is not None
    assert result.structured_output == {"outcome": "confirmed"}
