"""Create tenant accounts, provider integrations, API keys, and post-call jobs.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenant_accounts",
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("billing_mode", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_tenant_accounts")),
    )
    op.create_table(
        "provider_integrations",
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("encrypted_credentials", sa.Text(), nullable=False),
        sa.Column("settings_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "provider", "tenant_id", name=op.f("pk_provider_integrations")
        ),
    )
    op.create_table(
        "provider_phone_numbers",
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("number", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("inbound_enabled", sa.Boolean(), nullable=False),
        sa.Column("outbound_enabled", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider", "tenant_id"],
            ["provider_integrations.provider", "provider_integrations.tenant_id"],
            name=op.f("fk_provider_phone_numbers_provider_provider_integrations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "provider", "number", name=op.f("pk_provider_phone_numbers")
        ),
    )
    op.create_table(
        "post_call_jobs",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("channel", sa.String(length=40), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("encrypted_payload", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(length=200), nullable=True),
        sa.Column("encrypted_result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_post_call_jobs")),
    )
    op.create_index(
        "ix_post_call_jobs_status_lease_expires_at_created_at",
        "post_call_jobs",
        ["status", "lease_expires_at", "created_at"],
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("secret_hash", name=op.f("uq_api_keys_secret_hash")),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_tenant_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("ix_post_call_jobs_status_lease_expires_at_created_at", table_name="post_call_jobs")
    op.drop_table("post_call_jobs")
    op.drop_table("provider_phone_numbers")
    op.drop_table("provider_integrations")
    op.drop_table("tenant_accounts")
