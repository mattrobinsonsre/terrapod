"""enforce one task stage per (run, stage) (#742)

A run has exactly one stage per boundary (one post_plan, one pre_apply, …). The
idempotency in run_task_service.create_task_stage is a read-then-insert, which a
concurrent cross-replica insert can still race (the reconciler and the runner's
plan-result POST can both reach complete_plan and both find no stage). This adds
a DB-level UniqueConstraint(run_id, stage) so the invariant holds regardless.

Existing databases may already hold pre-#739 duplicate stages, so the upgrade
first dedupes — keeping the oldest stage per (run_id, stage) and cascade-deleting
the rest (task_stage_results.task_stage_id has ON DELETE CASCADE) — before adding
the constraint.

Revision ID: 5456426d7dd8
Revises: 5cf23a543399
Create Date: 2026-07-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "5456426d7dd8"
down_revision: str | None = "5cf23a543399"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Dedupe first: keep the oldest stage per (run_id, stage), drop the rest.
    # Their task_stage_results cascade-delete via the FK's ON DELETE CASCADE.
    op.execute(
        """
        DELETE FROM task_stages
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY run_id, stage
                           ORDER BY created_at ASC, id ASC
                       ) AS rn
                FROM task_stages
            ) ranked
            WHERE ranked.rn > 1
        )
        """
    )
    op.create_unique_constraint("uq_task_stages_run_stage", "task_stages", ["run_id", "stage"])


def downgrade() -> None:
    op.drop_constraint("uq_task_stages_run_stage", "task_stages", type_="unique")
