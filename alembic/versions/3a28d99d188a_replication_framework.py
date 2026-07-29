"""replication outbox + cursors (#960 phase 3, #1110)

Revision ID: 3a28d99d188a
Revises: ec759f96c1b7
Create Date: 2026-07-29

Purely additive: two new tables, nothing altered or dropped, so an old replica
running alongside during a rolling upgrade is unaffected.
"""

import sqlalchemy as sa
from alembic import op

revision = "3a28d99d188a"
down_revision = "ec759f96c1b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "replication_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("entity_class", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("op", sa.String(length=10), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin_node", sa.String(length=255), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_replication_events_class_id",
        "replication_events",
        ["entity_class", "entity_id"],
    )

    op.create_table(
        "replication_cursors",
        sa.Column("entity_class", sa.String(length=64), nullable=False),
        sa.Column("position", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("backfilling", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("entity_class"),
    )


def downgrade() -> None:
    op.drop_table("replication_cursors")
    op.drop_index("ix_replication_events_class_id", table_name="replication_events")
    op.drop_table("replication_events")
