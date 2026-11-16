"""Add reusable prompt templates and per API-key template assignments."""

from alembic import op
import sqlalchemy as sa

revision = "0026_agent_templates"
down_revision = "0025_post_call_gemini_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_templates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("tools_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("variables_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("settings_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_templates_kind", "agent_templates", ["kind"])
    op.create_table(
        "api_key_agent_templates",
        sa.Column("api_key_id", sa.String(length=32), nullable=False),
        sa.Column("template_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["template_id"], ["agent_templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("api_key_id", "template_id"),
    )
    op.create_index(
        "ix_api_key_agent_templates_default",
        "api_key_agent_templates",
        ["api_key_id", "kind"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )


def downgrade() -> None:
    op.drop_index("ix_api_key_agent_templates_default", table_name="api_key_agent_templates")
    op.drop_table("api_key_agent_templates")
    op.drop_index("ix_agent_templates_kind", table_name="agent_templates")
    op.drop_table("agent_templates")
