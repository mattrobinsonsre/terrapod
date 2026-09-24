"""cache the monthly cost delta on runs

Adds two nullable float columns to `runs`: `cost_diff_min` and
`cost_diff_max`, the monthly delta a run introduces (positive adds,
negative removes). The engine already computes this as `diff` in
`cost_estimate.json`; only `total` was being cached.

Null = the artifact carried no `diff` block (anything uploaded before
this), not zero — the same "don't know" vs "nothing" distinction the
resource-count columns make.

Purely additive and safe to reverse: both columns are a cache of a value
that is re-derivable from the stored `cost_estimate.json`, so the
downgrade drops them without data loss. Nothing reads them as a source
of truth.

Revision ID: c4d1e7b90a26
Revises: e2450c5ecc86
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d1e7b90a26"
# Re-parented for the 1.8 line. Upstream this sat on `e2450c5ecc86`, the head of
# the 2.0-bound chain, which this release line does not have. A release line's
# migrations must be a PREFIX of main's at the same positions, or a deployment
# upgrading off 1.8 meets them out of order and skips work. So on this line it
# follows 1.8's own head, and main re-parents to match rather than the reverse.
down_revision: str | None = "ede22f4eab33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("cost_diff_min", sa.Float(), nullable=True))
    op.add_column("runs", sa.Column("cost_diff_max", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "cost_diff_max")
    op.drop_column("runs", "cost_diff_min")
