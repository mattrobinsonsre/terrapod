"""Per-workspace resource requests: replace runner_definition with resource_cpu/resource_memory.

Revision ID: 0890044564cb
Revises: d14b3182f0bf
Create Date: 2026-02-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0890044564cb"
down_revision: Union[str, None] = "d14b3182f0bf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Workspaces: add resource columns, drop runner_definition ---
    op.add_column(
        "workspaces",
        sa.Column("resource_cpu", sa.String(20), nullable=False, server_default="1"),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "resource_memory", sa.String(20), nullable=False, server_default="2Gi"
        ),
    )
    op.drop_column("workspaces", "runner_definition")

    # --- Runs: add resource columns, drop runner_definition ---
    op.add_column(
        "runs",
        sa.Column("resource_cpu", sa.String(20), nullable=False, server_default="1"),
    )
    op.add_column(
        "runs",
        sa.Column(
            "resource_memory", sa.String(20), nullable=False, server_default="2Gi"
        ),
    )
    op.drop_column("runs", "runner_definition")


def downgrade() -> None:
    # --- Runs: restore runner_definition, drop resource columns ---
    op.add_column(
        "runs",
        sa.Column(
            "runner_definition",
            sa.String(63),
            nullable=False,
            server_default="standard",
        ),
    )
    op.drop_column("runs", "resource_memory")
    op.drop_column("runs", "resource_cpu")

    # --- Workspaces: restore runner_definition, drop resource columns ---
    op.add_column(
        "workspaces",
        sa.Column(
            "runner_definition",
            sa.String(63),
            nullable=False,
            server_default="standard",
        ),
    )
    op.drop_column("workspaces", "resource_memory")
    op.drop_column("workspaces", "resource_cpu")
