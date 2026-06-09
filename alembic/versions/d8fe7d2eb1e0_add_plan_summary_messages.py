"""Add plan_summary_messages table for the AI plan-summary chat thread.

One row per turn (user follow-up question OR assistant follow-up
reply) attached to an existing PlanSummary. The initial structured
summary stays on the `plan_summaries` row; this table only carries
the conversational follow-ups that build on top of it.

Per-row telemetry (input_tokens / output_tokens / model) so the
daily-budget gate can debit follow-up turns the same way it debits
the initial summary, and so an operator can audit cost per turn.

Revision ID: d8fe7d2eb1e0
Revises: 7e5df78aed8a
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "d8fe7d2eb1e0"
down_revision = "7e5df78aed8a"


def upgrade() -> None:
    op.create_table(
        "plan_summary_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "plan_summary_id",
            UUID(as_uuid=True),
            sa.ForeignKey("plan_summaries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
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
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_plan_summary_messages_role",
        ),
    )
    op.create_index(
        "ix_plan_summary_messages_plan_created",
        "plan_summary_messages",
        ["plan_summary_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_plan_summary_messages_plan_created", "plan_summary_messages")
    op.drop_table("plan_summary_messages")
