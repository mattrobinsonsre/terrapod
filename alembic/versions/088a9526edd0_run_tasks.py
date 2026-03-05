"""Run tasks: workspace-scoped webhook hooks for external validation.

Pre/post-plan/apply task stages that gate run progression based on
external service pass/fail callbacks.

Revision ID: 088a9526edd0
Revises: 305686df7bbd
Create Date: 2026-03-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "088a9526edd0"
down_revision: Union[str, None] = "305686df7bbd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── run_tasks (workspace-scoped task definitions) ─────────────────
    op.create_table(
        "run_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.String(2000), nullable=False),
        sa.Column("hmac_key_encrypted", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column(
            "enforcement_level",
            sa.String(20),
            nullable=False,
            server_default="mandatory",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_run_tasks_workspace_id", "run_tasks", ["workspace_id"])

    # ── task_stages (per-run stage execution instances) ───────────────
    op.create_table(
        "task_stages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_task_stages_run_id", "task_stages", ["run_id"])

    # ── task_stage_results (per-task result within a stage) ───────────
    op.create_table(
        "task_stage_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_stage_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("task_stages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("callback_token", sa.String(255), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_task_stage_results_task_stage_id",
        "task_stage_results",
        ["task_stage_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_stage_results_task_stage_id",
        table_name="task_stage_results",
    )
    op.drop_table("task_stage_results")
    op.drop_index("ix_task_stages_run_id", table_name="task_stages")
    op.drop_table("task_stages")
    op.drop_index("ix_run_tasks_workspace_id", table_name="run_tasks")
    op.drop_table("run_tasks")
