"""autodiscovery workspace lifecycle columns (#314)

workspaces.lifecycle_state/lifecycle_reason track the rename/delete/
orphan lifecycle (active|pending_deletion|archived);
workspaces.autodiscovery_pr_number records the PR that created an
autodiscovered workspace so the poller can reconcile when that PR is
closed-unmerged. autodiscovery_rules.on_directory_delete is the
opt-in destroy policy (default "flag" — never auto-destroys).

Existing rows default to active / "" / NULL / flag — no behaviour
change for anything already deployed.

Revision ID: 3f0ad398be2c
Revises: 3547bfb4e898
Create Date: 2026-05-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f0ad398be2c"
down_revision: str | None = "3547bfb4e898"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "lifecycle_state",
            sa.String(20),
            nullable=False,
            server_default="active",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("lifecycle_reason", sa.String(500), nullable=False, server_default=""),
    )
    op.add_column(
        "workspaces",
        sa.Column("autodiscovery_pr_number", sa.Integer(), nullable=True),
    )
    op.add_column(
        "autodiscovery_rules",
        sa.Column(
            "on_directory_delete",
            sa.String(10),
            nullable=False,
            server_default="flag",
        ),
    )


def downgrade() -> None:
    op.drop_column("autodiscovery_rules", "on_directory_delete")
    op.drop_column("workspaces", "autodiscovery_pr_number")
    op.drop_column("workspaces", "lifecycle_reason")
    op.drop_column("workspaces", "lifecycle_state")
