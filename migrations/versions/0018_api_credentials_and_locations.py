"""Store Vertex credentials on APIs and allow per-API agent locations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_api_agent_locations"
down_revision: str | None = "0017_agent_provider_configs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("voice_location", sa.String(length=64), nullable=True))
    op.add_column("api_keys", sa.Column("post_call_location", sa.String(length=64), nullable=True))

    # 0017 moved existing encrypted API credentials onto the assigned agent
    # configurations. Restore them to the API before removing those columns.
    op.execute(sa.text("""
        UPDATE api_keys k SET
            encrypted_vertex_credentials = coalesce(
                (SELECT c.encrypted_credentials FROM agent_provider_configs c WHERE c.id = k.voice_agent_id),
                (SELECT c.encrypted_credentials FROM agent_provider_configs c WHERE c.id = k.post_call_agent_id)
            ),
            vertex_project_id = coalesce(
                (SELECT c.project_id FROM agent_provider_configs c WHERE c.id = k.voice_agent_id),
                (SELECT c.project_id FROM agent_provider_configs c WHERE c.id = k.post_call_agent_id)
            ),
            vertex_location = coalesce(
                (SELECT c.location FROM agent_provider_configs c WHERE c.id = k.voice_agent_id),
                (SELECT c.location FROM agent_provider_configs c WHERE c.id = k.post_call_agent_id),
                k.vertex_location
            ),
            voice_location = coalesce(
                (SELECT c.location FROM agent_provider_configs c WHERE c.id = k.voice_agent_id),
                k.vertex_location
            ),
            post_call_location = coalesce(
                (SELECT c.location FROM agent_provider_configs c WHERE c.id = k.post_call_agent_id),
                k.vertex_location
            )
    """))
    op.drop_column("agent_provider_configs", "encrypted_credentials")
    op.drop_column("agent_provider_configs", "project_id")


def downgrade() -> None:
    op.add_column("agent_provider_configs", sa.Column("project_id", sa.String(length=128), nullable=True))
    op.add_column("agent_provider_configs", sa.Column("encrypted_credentials", sa.Text(), nullable=True))
    op.execute(sa.text("""
        UPDATE agent_provider_configs c SET
            project_id = k.vertex_project_id,
            encrypted_credentials = k.encrypted_vertex_credentials
        FROM api_keys k
        WHERE c.id = k.voice_agent_id OR c.id = k.post_call_agent_id
    """))
    op.drop_column("api_keys", "post_call_location")
    op.drop_column("api_keys", "voice_location")
