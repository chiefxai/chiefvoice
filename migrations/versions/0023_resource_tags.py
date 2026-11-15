"""Add tags to phone numbers, agent templates, and customer APIs."""

from alembic import op
import sqlalchemy as sa

revision = "0023_resource_tags"
down_revision = "0022_number_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("provider_phone_numbers", "agent_provider_configs", "api_keys"):
        op.add_column(
            table,
            sa.Column("tags_json", sa.Text(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    for table in ("api_keys", "agent_provider_configs", "provider_phone_numbers"):
        op.drop_column(table, "tags_json")
