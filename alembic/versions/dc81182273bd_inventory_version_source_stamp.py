"""inventory_versions.source_stamp -- the equality token a live read compares

Revision ID: dc81182273bd
Revises: 7f1b648caf08
Create Date: 2026-10-08

Expand-only: one NOT NULL column with a server default, so an old replica
writing a row during a rolling upgrade produces the empty stamp, which never
matches and is therefore re-resolved rather than trusted. Every row predating
this column lands in the same state for the same reason.

The column is what lets a read of the resolved view be live without writing a
row each time. Resolution is a database query for the one implemented source
kind, so a read can resolve every time -- but recording a version per read
would evict the bounded history a partial-configure retry subtracts against
(#1973). Comparing the stamp answers "has anything changed" without writing.
"""

import sqlalchemy as sa
from alembic import op

revision = "dc81182273bd"
down_revision = "7f1b648caf08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inventory_versions",
        sa.Column("source_stamp", sa.String(length=128), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("inventory_versions", "source_stamp")
