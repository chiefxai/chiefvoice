"""Allow one API key to own multiple exclusive phone numbers."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_api_key_phone_numbers"
down_revision: str | None = "0010_api_key_phone_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_key_phone_numbers",
        sa.Column("api_key_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("phone_number", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("api_key_id", "provider", "phone_number"),
    )
    op.create_index(
        "uq_api_key_phone_numbers_provider_number",
        "api_key_phone_numbers",
        ["provider", "phone_number"],
        unique=True,
    )
    op.create_index(
        "ix_api_key_phone_numbers_api_key_id", "api_key_phone_numbers", ["api_key_id"]
    )
    op.execute(
        """
        INSERT INTO api_key_phone_numbers (api_key_id, provider, phone_number)
        SELECT DISTINCT ON (assigned_phone_number)
            id, assigned_provider, assigned_phone_number
        FROM api_keys
        WHERE assigned_provider IS NOT NULL
            AND assigned_phone_number IS NOT NULL
            AND revoked_at IS NULL
        ORDER BY assigned_phone_number, created_at ASC, id ASC
        """
    )
    op.drop_constraint(
        op.f("ck_api_keys_api_key_phone_binding_pair"), "api_keys", type_="check"
    )
    op.drop_column("api_keys", "assigned_phone_number")
    op.drop_column("api_keys", "assigned_provider")


def downgrade() -> None:
    op.add_column("api_keys", sa.Column("assigned_provider", sa.String(length=40), nullable=True))
    op.add_column(
        "api_keys", sa.Column("assigned_phone_number", sa.String(length=64), nullable=True)
    )
    op.execute(
        """
        UPDATE api_keys AS keys
        SET assigned_provider = bindings.provider,
            assigned_phone_number = bindings.phone_number
        FROM (
            SELECT api_key_id, min(provider) AS provider, min(phone_number) AS phone_number
            FROM api_key_phone_numbers
            GROUP BY api_key_id
            HAVING count(*) = 1
        ) AS bindings
        WHERE keys.id = bindings.api_key_id
        """
    )
    op.create_check_constraint(
        op.f("ck_api_keys_api_key_phone_binding_pair"),
        "api_keys",
        "(assigned_provider IS NULL) = (assigned_phone_number IS NULL)",
    )
    op.drop_index("ix_api_key_phone_numbers_api_key_id", table_name="api_key_phone_numbers")
    op.drop_index(
        "uq_api_key_phone_numbers_provider_number", table_name="api_key_phone_numbers"
    )
    op.drop_table("api_key_phone_numbers")
