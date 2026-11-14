"""Add durable Vobiz callback nonce replay protection.

Revision ID: 0008_vobiz_callback_nonces
Revises: 0007_durable_call_sessions
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008_vobiz_callback_nonces"
down_revision: Union[str, None] = "0007_durable_call_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "vobiz_callback_nonces",
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "nonce", name=op.f("pk_vobiz_callback_nonces")),
    )
    op.create_index(
        "ix_vobiz_callback_nonces_created_at",
        "vobiz_callback_nonces",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_vobiz_callback_nonces_created_at", table_name="vobiz_callback_nonces")
    op.drop_table("vobiz_callback_nonces")
