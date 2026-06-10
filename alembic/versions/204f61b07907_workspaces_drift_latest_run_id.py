"""Add workspaces.drift_latest_run_id.

Records the run that produced the workspace's current drift_status —
lets the workspace-list UI link the "Drifted" / "Errored" status
badge directly to the drift detection run that produced it (so
operators can click through and see the plan output instead of
hunting through the runs list).

Set by `drift_detection_service.handle_drift_run_completed` on every
non-cancelled drift run completion (planned/no_drift, planned/drifted,
errored). Null when drift detection has never run (or was dismissed
via the dismiss-drift endpoint).

Plain nullable UUID — no FK. Drift runs may be GC'd by the run-
artifact retention sweep eventually, but we don't want a stale
drift_latest_run_id to break workspace deletion via FK cascade. The
UI handles 404 on click-through gracefully.

Revision ID: 204f61b07907
Revises: d8fe7d2eb1e0
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "204f61b07907"
down_revision = "d8fe7d2eb1e0"


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("drift_latest_run_id", UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "drift_latest_run_id")
