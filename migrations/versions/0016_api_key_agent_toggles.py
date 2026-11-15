"""Enable or disable per-API live and post-call agents."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_api_key_agent_toggles"
down_revision: str | None = "0015_api_key_agent_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("voice_agent_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("api_keys", sa.Column("post_call_agent_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("api_keys", "post_call_agent_enabled")
    op.drop_column("api_keys", "voice_agent_enabled")
