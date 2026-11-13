"""Add tenant post-call webhook configuration and delivery outbox.

Revision ID: 0002_post_call_webhook_delivery
Revises: 0001_initial_schema
Create Date: 2026-11-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_post_call_webhook_delivery"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "post_call_webhook_configs",
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_post_call_webhook_configs")),
    )
    op.create_table(
        "post_call_webhook_deliveries",
        sa.Column("event_id", sa.String(length=80), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error_type", sa.String(length=200), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_post_call_webhook_deliveries")),
        sa.UniqueConstraint("session_id", name=op.f("uq_post_call_webhook_deliveries_session_id")),
    )
    op.create_index(
        "ix_post_call_webhook_deliveries_status_lease_expires_at_next_attempt_at",
        "post_call_webhook_deliveries",
        ["status", "lease_expires_at", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_post_call_webhook_deliveries_status_lease_expires_at_next_attempt_at",
        table_name="post_call_webhook_deliveries",
    )
    op.drop_table("post_call_webhook_deliveries")
    op.drop_table("post_call_webhook_configs")
