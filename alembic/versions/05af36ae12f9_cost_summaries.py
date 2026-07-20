"""cost_summaries: AI cost-narrative enhancement, one-to-one with runs (#871)

Adds the ``cost_summaries`` table backing the optional AI *enhancement* of a
run's cost estimate. It rides the plan-analysis AI switch (``ai_summary.enabled``
+ the per-workspace mode) and holds AI *polish* only — a plain-language
narrative of the oiq-derived estimate plus optional savings advisories. It never
holds, restates, or replaces the authoritative oiq figures (those stay in the
``cost_estimate.json`` artifact + the ``runs`` cost columns); any dollar amount
in ``advisories`` is an AI estimate tagged ``source: "ai-estimate"``.

Stored separately from ``runs`` (same rationale as ``plan_summaries``): a
per-run, some-deployments-only feature must not grow the column footprint of a
large table, and the narrative text must not bloat cold reads. One-to-one with
Run via a unique ``run_id``; CASCADE on run delete.

Purely additive — a new table, no changes to existing tables, no backfill.

Revision ID: 05af36ae12f9
Revises: 238c16839feb
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "05af36ae12f9"
down_revision = "238c16839feb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cost_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "advisories",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("model", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_cost_summaries_run"),
    )


def downgrade() -> None:
    op.drop_table("cost_summaries")
