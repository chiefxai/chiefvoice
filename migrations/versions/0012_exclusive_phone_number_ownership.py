"""Make phone-number API ownership exclusive across all telephony providers."""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_phone_number_ownership"
down_revision: str | None = "0011_api_key_phone_numbers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "uq_api_key_phone_numbers_provider_number", table_name="api_key_phone_numbers"
    )
    op.create_index(
        "uq_api_key_phone_numbers_phone_number",
        "api_key_phone_numbers",
        ["phone_number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_api_key_phone_numbers_phone_number", table_name="api_key_phone_numbers")
    op.create_index(
        "uq_api_key_phone_numbers_provider_number",
        "api_key_phone_numbers",
        ["provider", "phone_number"],
        unique=True,
    )
