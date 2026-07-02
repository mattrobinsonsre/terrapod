"""plan staleness guards: state-version snapshot + discard reason + expiry TTL (#646/#647)

Adds the columns for two run-lifecycle staleness guards:
- ``runs.plan_state_serial`` (#647) — the workspace state-version serial a run
  planned against, so an apply can be blocked/auto-discarded when state moved.
- ``runs.discard_reason`` (#646/#647) — human-readable reason a run was discarded
  (state changed / plan expired / superseded).
- ``workspaces.plan_expiry_seconds`` (#646) — per-workspace TTL after which an
  unconfirmed plan is auto-discarded. NULL = disabled (default, current behaviour).

All three are nullable with no backfill: existing runs have no snapshot/reason
(so the state guard never retroactively fires on them) and existing workspaces
have expiry disabled — a faithful no-op on upgrade.

Revision ID: 65f5ee3a86be
Revises: a7e74cad11d5
"""

import sqlalchemy as sa
from alembic import op

revision = "65f5ee3a86be"
down_revision = "a7e74cad11d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("plan_state_serial", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("discard_reason", sa.String(length=200), nullable=True))
    op.add_column("workspaces", sa.Column("plan_expiry_seconds", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("workspaces", "plan_expiry_seconds")
    op.drop_column("runs", "discard_reason")
    op.drop_column("runs", "plan_state_serial")
