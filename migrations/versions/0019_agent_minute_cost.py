"""Add configured per-minute cost to agent configurations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_agent_minute_cost"
down_revision: str | None = "0018_api_agent_locations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_provider_configs",
        sa.Column("cost_per_minute_minor", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_provider_configs",
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
    )


def downgrade() -> None:
    op.drop_column("agent_provider_configs", "currency")
    op.drop_column("agent_provider_configs", "cost_per_minute_minor")
