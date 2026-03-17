"""Add module impact analysis tables and columns.

New table: module_workspace_links (link modules to consuming workspaces)
New column: registry_modules.vcs_last_pr_shas (PR dedup tracking)
New column: runs.module_overrides (module override storage paths per run)

Revision ID: 1b7bc2b4e011
Revises: 68bb56b372a1
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "1b7bc2b4e011"
down_revision = "68bb56b372a1"


def upgrade() -> None:
    # New junction table: link modules to consuming workspaces
    op.create_table(
        "module_workspace_links",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "module_id",
            UUID(as_uuid=True),
            sa.ForeignKey("registry_modules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.UniqueConstraint("module_id", "workspace_id", name="uq_module_workspace_links"),
    )
    op.create_index(
        "ix_module_workspace_links_module_id",
        "module_workspace_links",
        ["module_id"],
    )
    op.create_index(
        "ix_module_workspace_links_workspace_id",
        "module_workspace_links",
        ["workspace_id"],
    )

    # Track PR SHAs for dedup on module PR polling
    op.add_column(
        "registry_modules",
        sa.Column("vcs_last_pr_shas", JSONB, nullable=True),
    )

    # Module overrides per run (maps module coords to override storage paths)
    op.add_column(
        "runs",
        sa.Column("module_overrides", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "module_overrides")
    op.drop_column("registry_modules", "vcs_last_pr_shas")
    op.drop_index("ix_module_workspace_links_workspace_id", "module_workspace_links")
    op.drop_index("ix_module_workspace_links_module_id", "module_workspace_links")
    op.drop_table("module_workspace_links")
