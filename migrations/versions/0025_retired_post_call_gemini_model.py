"""Move Vertex post-call agents off retired Gemini 2.5 models."""

from collections.abc import Sequence

from alembic import op

revision: str = "0025_post_call_gemini_model"
down_revision: str | None = "0024_call_agent_hangup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_REPLACEMENTS = {
    "gemini-2.5-flash": "gemini-3.5-flash-lite",
    "gemini-2.5-flash-lite": "gemini-3.5-flash-lite",
    "gemini-2.5-pro": "gemini-3.5-flash",
}


def upgrade() -> None:
    # Gemini 2.5 Flash family retired on Vertex AI on 2026-10-20. Use the
    # GA Flash-Lite replacement and its global endpoint for existing post-call
    # templates and API-key fallback configurations.
    for old_model, replacement in _REPLACEMENTS.items():
        op.execute(
            "UPDATE agent_provider_configs "
            f"SET model = '{replacement}', location = 'global' "
            "WHERE kind = 'post_call' AND provider = 'vertex' "
            f"AND model = '{old_model}'"
        )
        op.execute(
            "UPDATE api_keys "
            f"SET post_call_model = '{replacement}', post_call_location = 'global' "
            "WHERE post_call_provider = 'vertex' "
            f"AND post_call_model = '{old_model}'"
        )


def downgrade() -> None:
    # Do not revert to a model that is retired and unavailable for new calls.
    pass
