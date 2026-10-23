"""per-host results and events for a configure run (#1928)

Expand-only: two new tables and nothing touched. A replica running the previous
release simply does not read them, so this is safe mid-rolling-upgrade.

Revision ID: 56e69e603265
Revises: bafb1c60d9fc
Create Date: 2026-10-23

Both tables are run-scoped and cascade from `runs`, which is what makes them
exempt from replication: `runs` is deliberately not replicated, so a follower
has no run for these to hang from. The event stream itself is an object under
`runs/`, already covered by the `run_vars` blob class.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "56e69e603265"
down_revision: str | None = "bafb1c60d9fc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_host_results",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("run_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("ok", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("changed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unreachable", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rescued", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ignored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("saw_unreachable", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "host", name="uq_run_host_results_run_host"),
    )
    op.create_index("ix_run_host_results_run", "run_host_results", ["run_id"])

    op.create_table(
        "run_host_events",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("run_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("task_name", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("task_action", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("task_uuid", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("play_name", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("ignored", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("msg", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload_ref", sa.Integer(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_run_host_events_run_host", "run_host_events", ["run_id", "host"])
    # The cross-run "has this task ever failed" query.
    op.create_index("ix_run_host_events_task", "run_host_events", ["workspace_id", "task_name"])


def downgrade() -> None:
    op.drop_index("ix_run_host_events_task", table_name="run_host_events")
    op.drop_index("ix_run_host_events_run_host", table_name="run_host_events")
    op.drop_table("run_host_events")
    op.drop_index("ix_run_host_results_run", table_name="run_host_results")
    op.drop_table("run_host_results")
