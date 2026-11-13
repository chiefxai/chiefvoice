"""Add tenant voice plans and idempotent usage ledgers.

Revision ID: 0004_voice_billing
Revises: 0003_agent_profiles
Create Date: 2026-11-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_voice_billing"
down_revision: Union[str, None] = "0003_agent_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenant_voice_plans",
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("billing_mode", sa.String(length=20), nullable=False),
        sa.Column("voice_enabled", sa.Boolean(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("rate_per_minute_minor", sa.Integer(), nullable=False),
        sa.Column("max_call_seconds", sa.Integer(), nullable=False),
        sa.Column("allowed_channels", sa.JSON(), nullable=False),
        sa.Column("max_concurrent_sessions", sa.Integer(), nullable=False),
        sa.Column("recording_allowed", sa.Boolean(), nullable=False),
        sa.Column("prepaid_balance_minor", sa.BigInteger(), nullable=False),
        sa.Column("postpaid_credit_limit_minor", sa.BigInteger(), nullable=False),
        sa.Column("postpaid_used_minor", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "billing_mode IN ('prepaid', 'postpaid')", name="billing_mode_valid"
        ),
        sa.CheckConstraint("rate_per_minute_minor > 0", name="rate_positive"),
        sa.CheckConstraint(
            "max_call_seconds BETWEEN 1 AND 3600", name="max_call_seconds_in_range"
        ),
        sa.CheckConstraint("max_concurrent_sessions > 0", name="max_concurrent_positive"),
        sa.CheckConstraint("prepaid_balance_minor >= 0", name="prepaid_balance_nonnegative"),
        sa.CheckConstraint(
            "postpaid_credit_limit_minor >= 0", name="postpaid_limit_nonnegative"
        ),
        sa.CheckConstraint("postpaid_used_minor >= 0", name="postpaid_used_nonnegative"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant_accounts.tenant_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    op.create_table(
        "voice_session_reservations",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("billing_mode", sa.String(length=20), nullable=False),
        sa.Column("channel", sa.String(length=40), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("rate_per_minute_minor", sa.Integer(), nullable=False),
        sa.Column("reserved_minor", sa.BigInteger(), nullable=False),
        sa.Column("max_call_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "duration_limit_event_reported", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("billable_seconds", sa.Integer(), nullable=True),
        sa.Column("charged_minor", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "billing_mode IN ('prepaid', 'postpaid')", name="billing_mode_valid"
        ),
        sa.CheckConstraint("reserved_minor >= 0", name="reserved_nonnegative"),
        sa.CheckConstraint(
            "status IN ('reserved', 'released', 'settled', 'settled_over_limit')",
            name="status_valid",
        ),
        sa.CheckConstraint("billable_seconds IS NULL OR billable_seconds >= 0", name="seconds_nonnegative"),
        sa.CheckConstraint("charged_minor IS NULL OR charged_minor >= 0", name="charge_nonnegative"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant_accounts.tenant_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        "ix_voice_session_reservations_tenant_id_status",
        "voice_session_reservations",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_voice_session_reservations_status_created_at",
        "voice_session_reservations",
        ["status", "created_at"],
    )
    op.create_table(
        "billing_ledger_entries",
        sa.Column("entry_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("billing_mode", sa.String(length=20), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("entry_type", sa.String(length=40), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "billing_mode IN ('prepaid', 'postpaid')", name="billing_mode_valid"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant_accounts.tenant_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["voice_session_reservations.session_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("entry_id"),
        sa.UniqueConstraint("session_id", "entry_type", name="uq_billing_ledger_session_event"),
    )
    op.create_index(
        "ix_billing_ledger_entries_tenant_id_occurred_at",
        "billing_ledger_entries",
        ["tenant_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_ledger_entries_tenant_id_occurred_at", table_name="billing_ledger_entries"
    )
    op.drop_table("billing_ledger_entries")
    op.drop_index(
        "ix_voice_session_reservations_tenant_id_status",
        table_name="voice_session_reservations",
    )
    op.drop_index(
        "ix_voice_session_reservations_status_created_at",
        table_name="voice_session_reservations",
    )
    op.drop_table("voice_session_reservations")
    op.drop_table("tenant_voice_plans")
