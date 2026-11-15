"""Move shared agents and resource inventory to platform ownership."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_platform_owned_resources"
down_revision: str | None = "0020_agent_costs_inr"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("""
        INSERT INTO tenant_accounts (tenant_id, display_name, billing_mode, created_by, created_at)
        VALUES ('platform', 'Platform', 'prepaid', 'system', now())
        ON CONFLICT (tenant_id) DO NOTHING
    """))
    op.drop_constraint(
        "uq_agent_provider_configs_tenant_kind_name",
        "agent_provider_configs",
        type_="unique",
    )
    op.execute(sa.text("UPDATE agent_provider_configs SET tenant_id = 'platform'"))
