"""Allow inbound agents to be selected per provider phone number.

Revision ID: 0009_phone_number_agent_routing
Revises: 0008_vobiz_callback_nonces
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009_phone_number_agent_routing"
down_revision: Union[str, None] = "0008_vobiz_callback_nonces"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "provider_phone_numbers",
        sa.Column("agent_id", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("provider_phone_numbers", "agent_id")
