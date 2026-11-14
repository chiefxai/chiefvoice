"""Track asynchronous post-call compute dispatch and recovery.

Revision ID: 0005_post_call_lambda_dispatch
Revises: 0004_voice_billing
Create Date: 2026-11-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005_post_call_lambda_dispatch"
down_revision: Union[str, None] = "0004_voice_billing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("post_call_jobs", sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "post_call_jobs",
        sa.Column("dispatch_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("post_call_jobs", sa.Column("last_dispatch_error_type", sa.String(length=200), nullable=True))
    op.create_index(
        "ix_post_call_jobs_status_next_attempt_at_last_dispatched_at",
        "post_call_jobs",
        ["status", "next_attempt_at", "last_dispatched_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_post_call_jobs_status_next_attempt_at_last_dispatched_at", table_name="post_call_jobs")
    op.drop_column("post_call_jobs", "last_dispatch_error_type")
    op.drop_column("post_call_jobs", "dispatch_attempts")
    op.drop_column("post_call_jobs", "last_dispatched_at")
