"""Persist platform-owned post-call assessment and recording STT state.

Revision ID: 0030_post_call_pipeline_assessment
Revises: 0029_custom_failure_webhook
Create Date: 2026-11-21 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_post_call_pipeline_assessment"
down_revision = "0029_custom_failure_webhook"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "post_call_jobs",
        sa.Column("transcript_status", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "post_call_jobs",
        sa.Column("stt_status", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "post_call_jobs",
        sa.Column("decision_reason", sa.String(length=80), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("post_call_jobs", "decision_reason")
    op.drop_column("post_call_jobs", "stt_status")
    op.drop_column("post_call_jobs", "transcript_status")
