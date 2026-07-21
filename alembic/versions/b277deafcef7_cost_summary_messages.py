"""cost_summary_messages — AI cost-estimate chat thread (#871)

Revision ID: b277deafcef7
Revises: d321124f68ee
Create Date: 2026-07-21

Adds the chat-thread table for the AI cost estimate, mirroring
``plan_summary_messages``. One row per conversational turn (operator question /
model reply), FK to ``cost_summaries`` with CASCADE so a deleted summary takes
its thread with it. Additive + reversible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "b277deafcef7"
down_revision: str | None = "d321124f68ee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cost_summary_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "cost_summary_id",
            UUID(as_uuid=True),
            sa.ForeignKey("cost_summaries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_cost_summary_messages_role",
        ),
    )
    op.create_index(
        "ix_cost_summary_messages_cost_created",
        "cost_summary_messages",
        ["cost_summary_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_cost_summary_messages_cost_created", table_name="cost_summary_messages")
    op.drop_table("cost_summary_messages")
