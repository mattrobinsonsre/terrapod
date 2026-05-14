"""add autodiscovery_rules.first_scan_at for initial-scan tracking (#309)

NULL means the rule has never been backfilled — the poll cycle picks
those up for a full-tree walk (in addition to its normal changed-files
walk) so existing matching directories get workspaces without waiting
for someone to touch each one. Cleared back to NULL whenever the rule
is re-enabled so a disable → enable cycle re-scans.

Revision ID: c3b957ded26c
Revises: 00b4c01e70e7
Create Date: 2026-05-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3b957ded26c"
down_revision: str | None = "00b4c01e70e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "autodiscovery_rules",
        sa.Column("first_scan_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("autodiscovery_rules", "first_scan_at")
