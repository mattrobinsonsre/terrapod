"""workspace hard_limit: a --limit no run may escape

Revision ID: 2e06ea7f06e4
Revises: 72376d0b8081
Create Date: 2026-11-09

`default_limit` is a DEFAULT -- `engines/terraform.py` reads
`ansible-limit OR default-limit`, so a run carrying its own limit replaces it
outright and can target any host in the resolved inventory. This is the ceiling
that cannot be escaped, intersected with whatever limit the run ends up with.

Expand-only: a new non-null column with a server default of the empty string,
which is "no ceiling" -- so every existing workspace behaves exactly as before
and no backfill is needed.
"""

import sqlalchemy as sa
from alembic import op

revision = "2e06ea7f06e4"
down_revision = "72376d0b8081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("hard_limit", sa.String(length=500), nullable=False, server_default=""),
    )
    # Both templates stamp it onto the workspaces they produce, and a ceiling
    # matters more there than on a hand-made workspace: a rule creates them in
    # bulk with nobody looking at each one, and a catalog item is picked by a
    # provisioner who never sees the settings it carries.
    for table in ("autodiscovery_rules", "catalog_items"):
        op.add_column(
            table,
            sa.Column("hard_limit", sa.String(length=500), nullable=False, server_default=""),
        )


def downgrade() -> None:
    for table in ("catalog_items", "autodiscovery_rules", "workspaces"):
        op.drop_column(table, "hard_limit")
