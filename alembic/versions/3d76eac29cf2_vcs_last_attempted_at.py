"""Add workspaces.vcs_last_attempted_at

`vcs_last_polled_at` only advances on a successful poll, so a workspace whose
polls fail every cycle has a frozen timestamp that is indistinguishable from one
that simply is not due yet. This column is stamped at the top of every poll
attempt, before anything can fail, so a monitor can detect a stalled workspace
from the API alone — even in the cases where the error field was never written
(#1089).

Purely additive: nullable, no backfill (NULL honestly means "not attempted since
this upgrade"), no contraction.

Revision ID: 3d76eac29cf2
Revises: 598eb2329821
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3d76eac29cf2"
down_revision: str | None = "598eb2329821"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("vcs_last_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "vcs_last_attempted_at")
