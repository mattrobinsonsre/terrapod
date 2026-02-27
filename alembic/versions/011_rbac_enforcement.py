"""RBAC enforcement: workspace labels/owner, role permissions, registry labels/owner.

Adds label-based RBAC columns to workspaces, registry modules, and registry
providers. Adds workspace_permission to custom roles for hierarchical
permission levels (read/plan/write/admin).

Revision ID: 011
Revises: 010
Create Date: 2026-02-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Workspace RBAC columns ---
    op.add_column(
        "workspaces",
        sa.Column(
            "labels",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("owner_email", sa.String(255), nullable=False, server_default=""),
    )

    # --- Role workspace_permission column ---
    op.add_column(
        "roles",
        sa.Column(
            "workspace_permission",
            sa.String(20),
            nullable=False,
            server_default="read",
        ),
    )

    # --- Registry module RBAC columns ---
    op.add_column(
        "registry_modules",
        sa.Column(
            "labels",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "registry_modules",
        sa.Column("owner_email", sa.String(255), nullable=False, server_default=""),
    )

    # --- Registry provider RBAC columns ---
    op.add_column(
        "registry_providers",
        sa.Column(
            "labels",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "registry_providers",
        sa.Column("owner_email", sa.String(255), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("registry_providers", "owner_email")
    op.drop_column("registry_providers", "labels")
    op.drop_column("registry_modules", "owner_email")
    op.drop_column("registry_modules", "labels")
    op.drop_column("roles", "workspace_permission")
    op.drop_column("workspaces", "owner_email")
    op.drop_column("workspaces", "labels")
