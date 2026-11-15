"""Add opt-in agent hangup behavior to calling templates."""

from alembic import op
import sqlalchemy as sa

revision = "0024_call_agent_hangup"
down_revision = "0023_resource_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_provider_configs",
        sa.Column("auto_hangup_when_complete", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("agent_provider_configs", "auto_hangup_when_complete")
