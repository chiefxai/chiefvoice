"""Persist one Vertex AI project provisioning state per tenant.

Revision ID: 0006_tenant_vertex_projects
Revises: 0005_post_call_lambda_dispatch
Create Date: 2026-11-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006_tenant_vertex_projects"
down_revision: Union[str, None] = "0005_post_call_lambda_dispatch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenant_vertex_projects",
        sa.Column("tenant_id", sa.String(length=200), nullable=False),
        sa.Column("project_id", sa.String(length=30), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(length=200), nullable=True),
        sa.Column("budget_name", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_tenant_vertex_projects")),
        sa.UniqueConstraint("project_id", name=op.f("uq_tenant_vertex_projects_project_id")),
    )
    op.create_index(
        "ix_tenant_vertex_projects_status_next_attempt_at_lease_expires_at",
        "tenant_vertex_projects",
        ["status", "next_attempt_at", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tenant_vertex_projects_status_next_attempt_at_lease_expires_at",
        table_name="tenant_vertex_projects",
    )
    op.drop_table("tenant_vertex_projects")
