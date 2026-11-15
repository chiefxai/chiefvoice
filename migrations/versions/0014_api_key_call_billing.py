"""Attribute calls and voice billing reservations to the originating API key."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_api_key_call_billing"
down_revision: str | None = "0013_api_key_vertex_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("call_sessions", sa.Column("api_key_id", sa.String(length=32), nullable=True))
    op.create_index("ix_call_sessions_api_key_id", "call_sessions", ["api_key_id"])
    op.create_foreign_key(
        "fk_call_sessions_api_key_id_api_keys", "call_sessions", "api_keys",
        ["api_key_id"], ["id"], ondelete="SET NULL",
    )
    op.add_column("voice_session_reservations", sa.Column("api_key_id", sa.String(length=32), nullable=True))
    op.create_index("ix_voice_session_reservations_api_key_id", "voice_session_reservations", ["api_key_id"])
    op.create_foreign_key(
        "fk_voice_session_reservations_api_key_id_api_keys", "voice_session_reservations", "api_keys",
        ["api_key_id"], ["id"], ondelete="SET NULL",
    )
    op.add_column("billing_ledger_entries", sa.Column("api_key_id", sa.String(length=32), nullable=True))
    op.create_index("ix_billing_ledger_entries_api_key_id", "billing_ledger_entries", ["api_key_id"])
    op.create_foreign_key(
        "fk_billing_ledger_entries_api_key_id_api_keys", "billing_ledger_entries", "api_keys",
        ["api_key_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_billing_ledger_entries_api_key_id_api_keys", "billing_ledger_entries", type_="foreignkey")
    op.drop_index("ix_billing_ledger_entries_api_key_id", table_name="billing_ledger_entries")
    op.drop_column("billing_ledger_entries", "api_key_id")
    op.drop_constraint("fk_voice_session_reservations_api_key_id_api_keys", "voice_session_reservations", type_="foreignkey")
    op.drop_index("ix_voice_session_reservations_api_key_id", table_name="voice_session_reservations")
    op.drop_column("voice_session_reservations", "api_key_id")
    op.drop_constraint("fk_call_sessions_api_key_id_api_keys", "call_sessions", type_="foreignkey")
    op.drop_index("ix_call_sessions_api_key_id", table_name="call_sessions")
    op.drop_column("call_sessions", "api_key_id")
