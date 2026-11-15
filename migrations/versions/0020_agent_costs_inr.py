"""Use INR for configured per-minute agent costs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_agent_costs_inr"
down_revision: str | None = "0019_agent_minute_cost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE agent_provider_configs SET currency = 'INR'"))
    op.alter_column(
        "agent_provider_configs", "currency",
        existing_type=sa.String(length=3), server_default="INR",
    )


def downgrade() -> None:
    op.alter_column(
        "agent_provider_configs", "currency",
        existing_type=sa.String(length=3), server_default="USD",
    )
    op.execute(sa.text("UPDATE agent_provider_configs SET currency = 'USD'"))
