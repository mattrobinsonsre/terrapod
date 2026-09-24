"""AI policy gate: per-run evaluations + the per-workspace override (#1766).

Pure expand: one new table and one defaulted column, so a lagging replica
during a rolling upgrade neither sees nor needs them.

`ai_policy_mode` defaults to "default" and the gate itself defaults to
disabled, so an upgrade changes nothing until an operator both enables
`ai_summary.policy` and says what to block.

Revision ID: ede22f4eab33
Revises: e2450c5ecc86
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "ede22f4eab33"
down_revision: str | Sequence[str] | None = "30b3243d256e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_policy_evaluations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Snapshotted at evaluation time so a later config edit cannot
        # retroactively change how a recorded run was gated.
        sa.Column("enforcement_level", sa.String(20), nullable=False),
        sa.Column("risk_threshold", sa.String(20), nullable=False, server_default="off"),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("verdict", JSONB, nullable=False, server_default="{}"),
        sa.Column("risk_level", sa.String(20), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("overridden_by", sa.String(255), nullable=True),
        sa.Column("overridden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("run_id", name="uq_ai_policy_evaluations_run"),
    )
    op.create_index("ix_ai_policy_evaluations_run_id", "ai_policy_evaluations", ["run_id"])

    op.add_column(
        "workspaces",
        sa.Column(
            "ai_policy_mode",
            sa.String(10),
            nullable=False,
            server_default="default",
        ),
    )
    # Templated onto every workspace an autodiscovery rule materialises, per
    # the surface-parity rule (#1763).
    op.add_column(
        "autodiscovery_rules",
        sa.Column(
            "ai_policy_mode",
            sa.String(20),
            nullable=False,
            server_default="default",
        ),
    )


def downgrade() -> None:
    op.drop_column("autodiscovery_rules", "ai_policy_mode")
    op.drop_column("workspaces", "ai_policy_mode")
    op.drop_index("ix_ai_policy_evaluations_run_id", table_name="ai_policy_evaluations")
    op.drop_table("ai_policy_evaluations")
