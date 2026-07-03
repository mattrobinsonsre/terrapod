"""execution hooks (#619)

A library of reusable custom-shell hooks run inside the runner Job at fixed
points (pre_init/pre_plan/post_plan/pre_apply/post_apply). Hooks reach a
workspace only via explicit association (no global flag). Additive + off by
default (a workspace with no associations runs no hooks), so no behaviour change
for existing deployments.

Revision ID: 6cede1508fb7
Revises: 65f5ee3a86be
Create Date: 2026-07-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6cede1508fb7"
down_revision: str | None = "65f5ee3a86be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "execution_hooks",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("hook_point", sa.String(20), nullable=False),
        sa.Column("script", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
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
        sa.UniqueConstraint("name", name="uq_execution_hooks"),
    )
    op.create_index(
        "ix_execution_hooks_hook_point",
        "execution_hooks",
        ["hook_point"],
    )

    op.create_table(
        "execution_hook_workspaces",
        sa.Column(
            "hook_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("execution_hooks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "workspace_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index(
        "ix_execution_hook_workspaces_workspace_id",
        "execution_hook_workspaces",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_hook_workspaces_workspace_id",
        table_name="execution_hook_workspaces",
    )
    op.drop_table("execution_hook_workspaces")
    op.drop_index("ix_execution_hooks_hook_point", table_name="execution_hooks")
    op.drop_table("execution_hooks")
