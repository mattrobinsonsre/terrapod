"""bump default terraform_version 1.12 -> 1.13 (OpenTofu 1.13 GA, #2004)

Only the column server_default changes. Existing rows are NOT touched:
a workspace or rule explicitly pinned to 1.12 stays on 1.12 — the
default only applies to new rows that don't specify a version. The
ORM-side default and config default move in lockstep in the same
change.

Revision ID: 3214ad466305
Revises: 15a8b636ea54
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3214ad466305"
down_revision: str | None = "cea79e480688"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "workspaces",
        "terraform_version",
        existing_type=sa.String(20),
        existing_nullable=False,
        server_default="1.13",
    )
    op.alter_column(
        "autodiscovery_rules",
        "terraform_version",
        existing_type=sa.String(50),
        existing_nullable=False,
        server_default="1.13",
    )


def downgrade() -> None:
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
