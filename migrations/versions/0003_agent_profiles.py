"""Add tenant-scoped live voice agent profiles.

Revision ID: 0003_agent_profiles
Revises: 0002_post_call_webhook_delivery
Create Date: 2026-11-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_agent_profiles"
down_revision: Union[str, None] = "0002_post_call_webhook_delivery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("language", sa.String(length=35), nullable=False),
        sa.Column("voice", sa.String(length=80), nullable=False),
        sa.Column("speaking_style", sa.String(length=20), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_profiles")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_agent_profiles_tenant_id")),
    )
    op.create_index("ix_agent_profiles_tenant_id", "agent_profiles", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_profiles_tenant_id", table_name="agent_profiles")
    op.drop_table("agent_profiles")
