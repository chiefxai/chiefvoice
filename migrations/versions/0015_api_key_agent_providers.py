"""Persist live and post-call provider/model selections per API key."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_api_key_agent_providers"
down_revision: str | None = "0014_api_key_call_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("voice_provider", sa.String(length=40), nullable=False, server_default="vertex"))
    op.add_column("api_keys", sa.Column("voice_model", sa.String(length=160), nullable=False, server_default="google/gemini-live-2.5-flash-native-audio"))
    op.add_column("api_keys", sa.Column("post_call_provider", sa.String(length=40), nullable=False, server_default="vertex"))
    op.add_column("api_keys", sa.Column("post_call_model", sa.String(length=160), nullable=False, server_default="gemini-2.5-flash"))


def downgrade() -> None:
    op.drop_column("api_keys", "post_call_model")
    op.drop_column("api_keys", "post_call_provider")
    op.drop_column("api_keys", "voice_model")
    op.drop_column("api_keys", "voice_provider")
