"""Drift detection: per-workspace scheduled plan-only runs for change detection.

Adds drift detection columns to workspaces and drift/has_changes tracking to runs.

Revision ID: 017
Revises: 016
Create Date: 2026-03-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Workspace drift detection columns ──────────────────────────
    op.add_column(
        "workspaces",
        sa.Column("drift_detection_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "drift_detection_interval_seconds",
            sa.Integer(),
            nullable=False,
            server_default="86400",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("drift_last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("drift_status", sa.String(20), nullable=False, server_default=""),
    )

    # ── Run drift detection columns ────────────────────────────────
    op.add_column(
        "runs",
        sa.Column("is_drift_detection", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "runs",
        sa.Column("has_changes", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "has_changes")
    op.drop_column("runs", "is_drift_detection")
    op.drop_column("workspaces", "drift_status")
    op.drop_column("workspaces", "drift_last_checked_at")
    op.drop_column("workspaces", "drift_detection_interval_seconds")
    op.drop_column("workspaces", "drift_detection_enabled")
