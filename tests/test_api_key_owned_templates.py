from datetime import UTC, datetime
from typing import Any
import pytest

from chiefvoice.agents.template_store import (
    AgentTemplateRow,
    ApiKeyAgentTemplateRow,
    SqlAgentTemplateDirectory,
)
from chiefvoice.agents.templates import AgentTemplate, AgentTemplateBody, new_template_id, render_template
from chiefvoice.identity.api_keys import ApiKeyRow


class _FakeScalarResult:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None

    def __iter__(self):
        return iter(self._items)


class _FakeSession:
    def __init__(self, api_keys, templates, assignments):
        self.api_keys = api_keys
        self.templates = templates
        self.assignments = assignments
        self.added = []

    async def get(self, model, pk):
        if model is ApiKeyRow:
            return self.api_keys.get(pk)
        if model is AgentTemplateRow:
            return self.templates.get(pk)
        return None

    async def scalar(self, statement):
        result = await self.scalars(statement)
        return result.first()

    async def scalars(self, statement):
        text_clause = str(statement).lower()
        if "from agent_templates" in text_clause and "join api_key_agent_templates" in text_clause:
            matched = [t for t in self.templates.values() if t.enabled]
            return _FakeScalarResult(matched)
        if "from agent_templates" in text_clause:
            matched = [t for t in self.templates.values() if t.enabled]
            return _FakeScalarResult(matched)
        if "from api_key_agent_templates" in text_clause:
            matched = [a for a in self.assignments]
            if "is_default" in text_clause:
                matched = [a.template_id for a in matched if a.is_default]
            return _FakeScalarResult(matched)
        return _FakeScalarResult([])

    async def execute(self, statement):
        text_clause = str(statement).lower()
        if "update api_key_agent_templates" in text_clause:
            for a in self.assignments:
                a.is_default = False
        if "select" in text_clause and "join" in text_clause:
            matched = [
                (a, self.templates[a.template_id])
                for a in self.assignments
                if a.template_id in self.templates and self.templates[a.template_id].enabled
            ]
            return _FakeScalarResult(matched)
        return _FakeScalarResult([])

    def add(self, entity):
        self.added.append(entity)
        if isinstance(entity, AgentTemplateRow):
            self.templates[entity.id] = entity
        elif isinstance(entity, ApiKeyAgentTemplateRow):
            self.assignments.append(entity)

    async def flush(self):
        pass

    async def delete(self, entity):
        if isinstance(entity, ApiKeyAgentTemplateRow):
            if entity in self.assignments:
                self.assignments.remove(entity)
        elif isinstance(entity, AgentTemplateRow):
            self.templates.pop(entity.id, None)


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


class _SessionMaker:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return _SessionContext(self.session)

    def begin(self):
        return _SessionContext(self.session)


class _DirectoryWithMockSessions(SqlAgentTemplateDirectory):
    def __init__(self, fake_session):
        self._fake_session = fake_session
        self._sessions = _SessionMaker(fake_session)


def _make_template(
    name: str,
    kind: str = "calling",
    instructions: str = "Hello {{patient_name}}",
    tools: tuple[dict[str, str], ...] = (),
    variables: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> AgentTemplate:
    now = datetime.now(UTC)
    return AgentTemplate(
        template_id=new_template_id(),
        kind=kind,
        name=name,
        instructions=instructions,
        tools=tools,
        variables=variables or {"patient_name": {"type": "string", "required": True}},
        settings=settings or {},
        revision=1,
        enabled=True,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def mock_directory():
    now = datetime.now(UTC)
    api_keys = {
        "api_key_1": ApiKeyRow(
            id="api_key_1",
            tenant_id="tenant_1",
            name="API One",
            secret_hash="hash1",
            scopes_json="[]",
            created_by="admin",
            created_at=now,
        ),
    }
    templates = {}
    assignments = []
    fake_session = _FakeSession(api_keys, templates, assignments)
    directory = _DirectoryWithMockSessions(fake_session)
    return directory, fake_session


@pytest.mark.asyncio
async def test_create_for_api_sets_first_as_default(mock_directory):
    directory, fake_session = mock_directory
    t1 = _make_template("Calling 1", kind="calling")
    created1 = await directory.create_for_api("api_key_1", t1)
    
    assert created1.name == "Calling 1"
    assert created1.api_key_id == "api_key_1"
    assert created1.is_default is True
    assert len(fake_session.assignments) == 1
    assert fake_session.assignments[0].is_default is True


@pytest.mark.asyncio
async def test_template_body_validation_and_rendering():
    body = AgentTemplateBody(
        name="Calling Flow",
        kind="calling",
        instructions="Welcome {{name}}, your balance is {{balance}}.",
        tools=[{"type": "builtin", "name": "end_call"}],
        variables={
            "name": {"type": "string", "required": True},
            "balance": {"type": "string", "required": True},
        },
        settings={"auto_hangup_when_complete": True},
    )
    assert body.name == "Calling Flow"
    assert body.kind == "calling"

    template = AgentTemplate(
        template_id="tpl-1",
        kind=body.kind,
        name=body.name,
        instructions=body.instructions,
        tools=tuple(tool.model_dump() for tool in body.tools),
        variables=body.variables,
        settings=body.settings,
        revision=1,
        enabled=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        api_key_id="api_key_1",
        is_default=True,
    )
    rendered = render_template(template, {"name": "Alice", "balance": "₹500"})
    assert rendered == "Welcome Alice, your balance is ₹500."

    with pytest.raises(ValueError, match="Missing required template variable: balance"):
        render_template(template, {"name": "Alice"})


def test_agent_template_body_rejects_unsupported_tools():
    with pytest.raises(ValueError, match="Unsupported calling template tool"):
        AgentTemplateBody(
            name="Bad Tools",
            kind="calling",
            instructions="Hello",
            tools=[{"type": "builtin", "name": "calculator"}],
        )

    with pytest.raises(ValueError, match="Unsupported post_call template tool"):
        AgentTemplateBody(
            name="Bad Post Call",
            kind="post_call",
            instructions="Hello",
            tools=[{"type": "builtin", "name": "end_call"}],
        )


def test_agent_template_body_rejects_undeclared_variables():
    with pytest.raises(ValueError, match="Declare every instruction placeholder in variables: code"):
        AgentTemplateBody(
            name="Missing Var",
            kind="calling",
            instructions="Your code is {{code}}",
            variables={},
        )
