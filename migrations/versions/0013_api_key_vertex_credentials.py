"""Store encrypted Vertex configuration per API key."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_api_key_vertex_credentials"
down_revision: str | None = "0012_phone_number_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("vertex_project_id", sa.String(length=128), nullable=True))
    op.add_column("api_keys", sa.Column("vertex_location", sa.String(length=64), nullable=True))
    op.add_column("api_keys", sa.Column("encrypted_vertex_credentials", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "encrypted_vertex_credentials")
    op.drop_column("api_keys", "vertex_location")
    op.drop_column("api_keys", "vertex_project_id")
