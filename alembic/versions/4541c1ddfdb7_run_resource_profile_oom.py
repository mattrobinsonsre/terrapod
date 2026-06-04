"""Add runner resource-profile + OOM detection columns to runs (#430).

Captures the runner Job's actual resource usage (peak memory + CPU)
plus the K8s exit signal (exit code + termination reason), so the run
UI can surface "you're at 95% of your memory limit, increase it" before
the next OOM. See issue #430.

Columns added:
  peak_memory_bytes      — BIGINT, from /sys/fs/cgroup/memory.peak (cgroup v2)
  peak_cpu_usec          — BIGINT, from /sys/fs/cgroup/cpu.stat (usage_usec)
  runner_exit_code       — INT,    container exit code (137 = SIGKILL, often OOM)
  runner_exit_reason     — VARCHAR(50), K8s container.state.terminated.reason
                           ("OOMKilled", "Error", "Completed")
  runner_exit_status     — VARCHAR(20), Terrapod-side typed bucket:
                           "" / "clean" / "oom" / "killed" / "error"
                           Distinguishes "plan-result posted then runner died"
                           from "plan-result posted and runner exited cleanly".

All nullable / default-empty so existing runs need no backfill.

Revision ID: 4541c1ddfdb7
Revises: e25fce3f7b58
"""

import sqlalchemy as sa
from alembic import op

revision = "4541c1ddfdb7"
down_revision = "e25fce3f7b58"


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("peak_memory_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("peak_cpu_usec", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("runner_exit_code", sa.Integer(), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column(
            "runner_exit_reason",
            sa.String(50),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "runner_exit_status",
            sa.String(20),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "runner_exit_status")
    op.drop_column("runs", "runner_exit_reason")
    op.drop_column("runs", "runner_exit_code")
    op.drop_column("runs", "peak_cpu_usec")
    op.drop_column("runs", "peak_memory_bytes")
