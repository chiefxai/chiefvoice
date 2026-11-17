import asyncio
from datetime import datetime, UTC
import sys

from chiefvoice.agents.template_store import SqlAgentTemplateDirectory
from chiefvoice.agents.templates import AgentTemplate, new_template_id
from chiefvoice.core.config import settings


async def main():
    key_id = sys.argv[1] if len(sys.argv) > 1 else "b92bdfb222a05ab4b870083d"
    store = SqlAgentTemplateDirectory(settings.database_url)

    # Check existing assignments
    assignments = await store.list_api_assignments(key_id)
    default_id = assignments.get("default_calling_id")
    if default_id:
        print(f"Default calling template is already set: {default_id}")
        await store.close()
        return

    # Check if any templates exist in DB
    existing = await store.list_all("calling")
    if existing:
        t = existing[0]
        print(f"Assigning existing template '{t.name}' ({t.template_id}) to API {key_id}...")
        await store.set_api_assignments(
            api_key_id=key_id,
            calling_ids=[t.template_id],
            post_call_ids=[],
            default_calling_id=t.template_id,
            default_post_call_id=None,
        )
        print(f"Successfully assigned {t.template_id} as default calling template!")
    else:
        now = datetime.now(UTC)
        new_id = new_template_id()
        tpl = AgentTemplate(
            template_id=new_id,
            kind="calling",
            name="General Voice Assistant",
            instructions=(
                "You are a helpful AI voice assistant for {{customer_name}}. "
                "Speak naturally, be concise, and assist the user with their inquiry. "
                "When the user says goodbye or wants to end the call, call end_call."
            ),
            tools=({"name": "end_call", "type": "builtin"},),
            variables={"customer_name": {"type": "string", "required": True}},
            settings={"auto_hangup_when_complete": True},
            revision=1,
            enabled=True,
            created_at=now,
            updated_at=now,
            api_key_id=key_id,
            is_default=True,
        )
        created = await store.create_for_api(key_id, tpl, is_default=True)
        print(f"Created new default calling template: {created.template_id}")

    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
