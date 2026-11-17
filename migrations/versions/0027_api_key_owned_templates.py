"""Scope agent templates directly to API keys.

Revision ID: 0027_api_key_owned_templates
Revises: 0026_agent_templates
Create Date: 2026-11-17 01:30:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0027_api_key_owned_templates"
down_revision = "0026_agent_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_templates",
        sa.Column("api_key_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_templates_api_key_id_api_keys",
        "agent_templates",
        "api_keys",
        ["api_key_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_agent_templates_api_key_id",
        "agent_templates",
        ["api_key_id"],
    )
    op.execute(
        sa.text(
            """
            UPDATE agent_templates
            SET api_key_id = api_key_agent_templates.api_key_id
            FROM api_key_agent_templates
            WHERE agent_templates.id = api_key_agent_templates.template_id
              AND agent_templates.api_key_id IS NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_agent_templates_api_key_id", table_name="agent_templates")
    op.drop_constraint("fk_agent_templates_api_key_id_api_keys", table_name="agent_templates", type_="foreignkey")
    op.drop_column("agent_templates", "api_key_id")
