"""Support non-AI call and post-call failure webhook events.

Revision ID: 0028_failure_webhook_events
Revises: 0027_api_key_owned_templates
Create Date: 2026-11-18 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0028_failure_webhook_events"
down_revision = "0027_api_key_owned_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_post_call_webhook_deliveries_session_id",
        "post_call_webhook_deliveries",
        type_="unique",
    )
    op.create_index(
        "ix_post_call_webhook_deliveries_session_id",
        "post_call_webhook_deliveries",
        ["session_id"],
    )
    op.add_column(
        "post_call_webhook_deliveries",
        sa.Column(
            "event_type",
            sa.String(length=80),
            nullable=False,
            server_default="call.analysis.completed",
        ),
    )
    op.add_column(
        "post_call_webhook_deliveries",
        sa.Column("encrypted_event_payload", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("post_call_webhook_deliveries", "encrypted_event_payload")
    op.drop_column("post_call_webhook_deliveries", "event_type")
    op.drop_index(
        "ix_post_call_webhook_deliveries_session_id",
        table_name="post_call_webhook_deliveries",
    )
    op.create_unique_constraint(
        "uq_post_call_webhook_deliveries_session_id",
        "post_call_webhook_deliveries",
        ["session_id"],
    )
