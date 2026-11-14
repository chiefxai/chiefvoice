"""Bind an API credential to a provider phone number."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_api_key_phone_binding"
down_revision: str | None = "0009_phone_number_agent_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("assigned_provider", sa.String(length=40), nullable=True))
    op.add_column(
        "api_keys", sa.Column("assigned_phone_number", sa.String(length=64), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_api_keys_api_key_phone_binding_pair"),
        "api_keys",
        "(assigned_provider IS NULL) = (assigned_phone_number IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_api_keys_api_key_phone_binding_pair"), "api_keys", type_="check")
    op.drop_column("api_keys", "assigned_phone_number")
    op.drop_column("api_keys", "assigned_provider")
