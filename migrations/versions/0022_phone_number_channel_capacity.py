"""Track standard and unlimited channel capacity per platform phone number."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_phone_number_channel_capacity"
down_revision: str | None = "0021_platform_owned_resources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "provider_phone_numbers",
        sa.Column("standard_channels", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "provider_phone_numbers",
        sa.Column("unlimited_channels", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "phone_number_channel_capacity_nonnegative",
        "provider_phone_numbers",
        "standard_channels >= 0 AND unlimited_channels >= 0",
    )
    op.create_check_constraint(
        "phone_number_channel_capacity_positive",
        "provider_phone_numbers",
        "standard_channels + unlimited_channels > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "phone_number_channel_capacity_positive",
        "provider_phone_numbers",
        type_="check",
    )
    op.drop_constraint(
        "phone_number_channel_capacity_nonnegative",
        "provider_phone_numbers",
        type_="check",
    )
    op.drop_column("provider_phone_numbers", "unlimited_channels")
    op.drop_column("provider_phone_numbers", "standard_channels")
