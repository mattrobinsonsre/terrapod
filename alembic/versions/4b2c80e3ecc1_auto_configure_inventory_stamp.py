"""auto-configure on a non-git inventory change: the stamp it compares against

Revision ID: 4b2c80e3ecc1
Revises: 123a70ece5e0
Create Date: 2026-10-22

Expand-only: one nullable column, no backfill. NULL means "never auto-configured
from an inventory change", and the first cycle on an opted-in workspace stamps it
rather than firing -- see `inventory_auto_configure` for why that is the safe
direction on an existing deployment (#2224).
"""

import sqlalchemy as sa
from alembic import op

revision = "4b2c80e3ecc1"
down_revision = "123a70ece5e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("auto_configure_inventory_stamp", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "auto_configure_inventory_stamp")
