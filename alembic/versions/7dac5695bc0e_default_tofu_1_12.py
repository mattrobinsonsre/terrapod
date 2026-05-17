"""bump default terraform_version 1.11 -> 1.12 (OpenTofu 1.12 GA, #325)

Only the column server_default changes. Existing rows are NOT touched:
a workspace or rule explicitly pinned to 1.11 stays on 1.11 — the
default only applies to new rows that don't specify a version. The
ORM-side default and config default move in lockstep in the same
change.

Revision ID: 7dac5695bc0e
Revises: c3b957ded26c
Create Date: 2026-05-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7dac5695bc0e"
down_revision: str | None = "c3b957ded26c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "workspaces",
        "terraform_version",
        existing_type=sa.String(20),
        existing_nullable=False,
        server_default="1.12",
    )
    op.alter_column(
        "autodiscovery_rules",
        "terraform_version",
        existing_type=sa.String(50),
        existing_nullable=False,
        server_default="1.12",
    )


def downgrade() -> None:
    op.alter_column(
        "workspaces",
        "terraform_version",
        existing_type=sa.String(20),
        existing_nullable=False,
        server_default="1.11",
    )
    op.alter_column(
        "autodiscovery_rules",
        "terraform_version",
        existing_type=sa.String(50),
        existing_nullable=False,
        server_default="1.11",
    )
