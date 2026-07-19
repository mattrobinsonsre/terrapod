"""run cost estimate: has_cost_estimate + cached monthly total range (#871)

Adds the columns backing per-run cost estimation:
- ``runs.has_cost_estimate`` — set once the runner uploads ``cost_estimate.json``
  (the native OpenInfraQuote-port estimate of the plan's monthly cost delta),
  gating the download URL the same way ``has_json_output`` gates the plan JSON.
- ``runs.cost_currency`` / ``runs.cost_monthly_min`` / ``runs.cost_monthly_max``
  — the plan-total monthly cost range cached for cheap list display; the full
  per-resource breakdown lives in the stored artifact.

Purely additive: ``has_cost_estimate`` defaults false (existing runs advertise
no cost URL), the range columns are nullable with no backfill — a faithful
no-op on upgrade.

Revision ID: 238c16839feb
Revises: 7bb4d122cfd5
"""

import sqlalchemy as sa

from alembic import op

revision = "238c16839feb"
down_revision = "7bb4d122cfd5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "has_cost_estimate",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column("runs", sa.Column("cost_currency", sa.String(length=8), nullable=True))
    op.add_column("runs", sa.Column("cost_monthly_min", sa.Float(), nullable=True))
    op.add_column("runs", sa.Column("cost_monthly_max", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "cost_monthly_max")
    op.drop_column("runs", "cost_monthly_min")
    op.drop_column("runs", "cost_currency")
    op.drop_column("runs", "has_cost_estimate")
