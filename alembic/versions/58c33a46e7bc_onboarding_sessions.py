"""onboarding sessions (#824 P2)

Workspace-scoped resource-onboarding discovery sessions: discover existing,
unmanaged cloud resources (terrapod-query, #823) and generate copy-pasteable
``resource`` + ``import {}`` config, then a gated import-only run. Additive +
inert for existing deployments (nothing creates a session until the discovery
run type + endpoints land in a later phase), so no behaviour change.

Revision ID: 58c33a46e7bc
Revises: ca34d7deb54d
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "58c33a46e7bc"
down_revision: str | None = "ca34d7deb54d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "onboarding_sessions",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(20),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("provider", sa.String(64), server_default="", nullable=False),
        sa.Column("discovery_surface", postgresql.JSONB(), nullable=True),
        sa.Column(
            "selected_types",
            postgresql.JSONB(),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("query_results", postgresql.JSONB(), nullable=True),
        sa.Column("generated_config", sa.Text(), nullable=True),
        sa.Column("import_blocks", sa.Text(), nullable=True),
        sa.Column("discovery_run_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("result_run_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "ai_assisted",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), server_default="", nullable=False),
        sa.Column("created_by", sa.String(255), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_onboarding_sessions_workspace",
        "onboarding_sessions",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_onboarding_sessions_workspace", table_name="onboarding_sessions")
    op.drop_table("onboarding_sessions")
