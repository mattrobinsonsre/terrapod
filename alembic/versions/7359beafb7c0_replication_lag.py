"""Record how far behind replication is, not just how long ago it ran (#1165).

Two nullable columns on the event-stream cursor. Both are things the follower
cannot work out alone: its page from the leader is capped at `limit`, so a full
page means "there is more" and nothing at all about how much more. The leader
now reports its newest event id and the timestamp of the oldest event it is
handing back, and the follower keeps them here.

Nullable rather than defaulted to zero: a node that has never pulled, or whose
peer predates this, genuinely has no answer — and "0 events behind" would be a
confident lie in exactly the situation where an operator is about to act on it.

Purely additive. A node running older code ignores both columns; a node running
this one against a peer that does not send the fields leaves them null and says
"unknown" rather than inventing a number.

Revision ID: 7359beafb7c0
Revises: 3a28d99d188a
"""

import sqlalchemy as sa
from alembic import op

revision: str = "7359beafb7c0"
down_revision: str | None = "3a28d99d188a"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "replication_cursors",
        sa.Column("peer_latest_position", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "replication_cursors",
        sa.Column("oldest_unapplied_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("replication_cursors", "oldest_unapplied_at")
    op.drop_column("replication_cursors", "peer_latest_position")
