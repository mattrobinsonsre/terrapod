"""Add plan_summaries table and workspace AI-summary opt-in columns (#401).

Revision ID: e25fce3f7b58
Revises: c7d8e9f0a1b2
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "e25fce3f7b58"
down_revision = "c7d8e9f0a1b2"


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "ai_summary_mode",
            sa.String(10),
            nullable=False,
            server_default="default",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "ai_summary_context",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.create_check_constraint(
        "ck_workspaces_ai_summary_mode",
        "workspaces",
        "ai_summary_mode IN ('default', 'enabled', 'disabled')",
    )

    op.create_table(
        "plan_summaries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False, server_default="plan_summary"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("risk_level", sa.String(20), nullable=False, server_default=""),
        sa.Column("risk_factors", JSONB, nullable=False, server_default="[]"),
        sa.Column("model", sa.String(255), nullable=False, server_default=""),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("run_id", name="uq_plan_summaries_run"),
    )
    op.create_check_constraint(
        "ck_plan_summaries_status",
        "plan_summaries",
        "status IN ('pending', 'ready', 'skipped', 'errored')",
    )
    op.create_check_constraint(
        "ck_plan_summaries_kind",
        "plan_summaries",
        "kind IN ('plan_summary', 'failure_analysis')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_plan_summaries_kind", "plan_summaries", type_="check")
    op.drop_constraint("ck_plan_summaries_status", "plan_summaries", type_="check")
    op.drop_table("plan_summaries")
    op.drop_constraint("ck_workspaces_ai_summary_mode", "workspaces", type_="check")
    op.drop_column("workspaces", "ai_summary_context")
    op.drop_column("workspaces", "ai_summary_mode")
