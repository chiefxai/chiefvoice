"""Add durable outbound call queue and dispatch leases.

Revision ID: 0031_outbound_call_queue
Revises: 0030_post_call_assessment
Create Date: 2026-11-23 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0031_outbound_call_queue"
down_revision = "0030_post_call_assessment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "call_sessions",
        sa.Column("queue_enqueued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "call_sessions",
        sa.Column("queue_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "call_sessions",
        sa.Column("queue_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "outbound_call_queue",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="queued"
        ),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["call_sessions.session_id"],
            name=op.f("fk_outbound_call_queue_session_id_call_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_outbound_call_queue")),
    )
    op.create_index(
        "ix_outbound_call_queue_tenant_id",
        "outbound_call_queue",
        ["tenant_id"],
    )
    op.create_index(
        "ix_outbound_call_queue_status_enqueued",
        "outbound_call_queue",
        ["status", "enqueued_at"],
    )
    op.create_index(
        "ix_outbound_call_queue_status_lease",
        "outbound_call_queue",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbound_call_queue_status_lease", table_name="outbound_call_queue")
    op.drop_index("ix_outbound_call_queue_status_enqueued", table_name="outbound_call_queue")
    op.drop_index("ix_outbound_call_queue_tenant_id", table_name="outbound_call_queue")
    op.drop_table("outbound_call_queue")
    op.drop_column("call_sessions", "queue_attempts")
    op.drop_column("call_sessions", "queue_expires_at")
    op.drop_column("call_sessions", "queue_enqueued_at")
