"""Move provider credentials into reusable agent configurations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_agent_provider_configs"
down_revision: str | None = "0016_api_key_agent_toggles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_provider_configs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=200), sa.ForeignKey("tenant_accounts.tenant_id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("location", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("encrypted_credentials", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "kind", "name", name="uq_agent_provider_configs_tenant_kind_name"),
    )
    op.create_index("ix_agent_provider_configs_tenant_id", "agent_provider_configs", ["tenant_id"])
    op.add_column("api_keys", sa.Column("voice_agent_id", sa.String(length=32), nullable=True))
    op.add_column("api_keys", sa.Column("post_call_agent_id", sa.String(length=32), nullable=True))
    op.create_foreign_key("fk_api_keys_voice_agent_id_agent_provider_configs", "api_keys", "agent_provider_configs", ["voice_agent_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_api_keys_post_call_agent_id_agent_provider_configs", "api_keys", "agent_provider_configs", ["post_call_agent_id"], ["id"], ondelete="SET NULL")

    # Preserve existing API behavior by moving encrypted credentials/model settings
    # into agent records and assigning those records to each existing API key.
    op.execute(sa.text("""
        INSERT INTO agent_provider_configs
            (id, tenant_id, name, kind, provider, model, location, project_id,
             encrypted_credentials, enabled, created_at, updated_at)
        SELECT md5(k.id || chr(58) || 'calling'), k.tenant_id, 'Calling agent ' || left(k.id, 8),
               'calling', k.voice_provider, k.voice_model, coalesce(k.vertex_location, 'us-central1'),
               k.vertex_project_id, k.encrypted_vertex_credentials, true, k.created_at, k.created_at
        FROM api_keys k
        WHERE k.voice_agent_enabled = true AND k.vertex_project_id IS NOT NULL
          AND k.encrypted_vertex_credentials IS NOT NULL
    """))
    op.execute(sa.text("""
        INSERT INTO agent_provider_configs
            (id, tenant_id, name, kind, provider, model, location, project_id,
             encrypted_credentials, enabled, created_at, updated_at)
        SELECT md5(k.id || chr(58) || 'post-call'), k.tenant_id, 'Post-call agent ' || left(k.id, 8),
               'post_call', k.post_call_provider, k.post_call_model, coalesce(k.vertex_location, 'us-central1'),
               k.vertex_project_id, k.encrypted_vertex_credentials, true, k.created_at, k.created_at
        FROM api_keys k
        WHERE k.post_call_agent_enabled = true AND k.vertex_project_id IS NOT NULL
          AND k.encrypted_vertex_credentials IS NOT NULL
    """))
    op.execute(sa.text("""
        UPDATE api_keys SET voice_agent_id = md5(api_keys.id || chr(58) || 'calling')
        WHERE voice_agent_enabled = true AND vertex_project_id IS NOT NULL
          AND encrypted_vertex_credentials IS NOT NULL
    """))
    op.execute(sa.text("""
        UPDATE api_keys SET post_call_agent_id = md5(api_keys.id || chr(58) || 'post-call')
        WHERE post_call_agent_enabled = true AND vertex_project_id IS NOT NULL
          AND encrypted_vertex_credentials IS NOT NULL
    """))
    op.execute(sa.text("UPDATE api_keys SET encrypted_vertex_credentials = NULL"))


def downgrade() -> None:
    op.drop_constraint("fk_api_keys_post_call_agent_id_agent_provider_configs", "api_keys", type_="foreignkey")
    op.drop_constraint("fk_api_keys_voice_agent_id_agent_provider_configs", "api_keys", type_="foreignkey")
    op.drop_column("api_keys", "post_call_agent_id")
    op.drop_column("api_keys", "voice_agent_id")
    # Credentials copied into the new table cannot be safely reconstructed into
    # legacy API-key storage during downgrade. Keep the downgrade schema-only.
    op.drop_index("ix_agent_provider_configs_tenant_id", table_name="agent_provider_configs")
    op.drop_table("agent_provider_configs")
