"""Persist call lifecycle, idempotency keys, and encrypted transcript checkpoints.

Revision ID: 0007_durable_call_sessions
Revises: 0006_tenant_vertex_projects
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007_durable_call_sessions"
down_revision: Union[str, None] = "0006_tenant_vertex_projects"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "call_sessions",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provider_call_id", sa.String(length=256), nullable=True),
        sa.Column("encrypted_session", sa.Text(), nullable=True),
        sa.Column("encrypted_transcript", sa.Text(), nullable=True),
        sa.Column("transcript_characters", sa.Integer(), server_default="0", nullable=False),
        sa.Column("transcript_persistence_failed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=40), nullable=True),
        sa.Column("post_call_enqueued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_call_sessions")),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name=op.f("uq_call_sessions_tenant_id")),
    )
    op.create_index("ix_call_sessions_tenant_id", "call_sessions", ["tenant_id"])
    op.create_index("ix_call_sessions_status_ended_at", "call_sessions", ["status", "ended_at"])


def downgrade() -> None:
    op.drop_index("ix_call_sessions_status_ended_at", table_name="call_sessions")
    op.drop_index("ix_call_sessions_tenant_id", table_name="call_sessions")
    op.drop_table("call_sessions")
