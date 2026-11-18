"""Configure custom non-AI failure webhook delivery per API.

Revision ID: 0029_custom_failure_webhook
Revises: 0028_failure_webhook_events
Create Date: 2026-11-18 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0029_custom_failure_webhook"
down_revision = "0028_failure_webhook_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "post_call_webhook_configs",
        sa.Column("failure_target_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "post_call_webhook_configs",
        sa.Column("encrypted_failure_custom_payload", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("post_call_webhook_configs", "encrypted_failure_custom_payload")
    op.drop_column("post_call_webhook_configs", "failure_target_url")
