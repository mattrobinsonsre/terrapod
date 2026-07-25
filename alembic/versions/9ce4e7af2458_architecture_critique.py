"""architecture_critique — AI senior-architect critique per run (#963/#1036)

Revision ID: 9ce4e7af2458
Revises: 10e9f2f624ea
Create Date: 2026-07-25

Part 2 of the security-scanning epic (#1036): the AI *reasoning* layer that
renders on top of the deterministic Checkov/Trivy scan panel. A senior
cloud-architect critique of the run's *proposed* infrastructure (plan-JSON
``planned_values``), stored one row per run.

- ``architecture_critiques`` — the CostSummary twin (status, critique narrative,
  structured findings, overall risk level, telemetry), unique on ``run_id`` for
  ON CONFLICT idempotency.
- ``architecture_critique_messages`` — the follow-up chat thread (CostSummaryMessage
  twin), grounded in the same plan JSON.

Additive + reversible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "9ce4e7af2458"
down_revision: str | None = "10e9f2f624ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- one AI critique per run (twin of cost_summaries) ---
    op.create_table(
        "architecture_critiques",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("critique", sa.Text(), nullable=False, server_default=""),
        sa.Column("findings", JSONB(), nullable=False, server_default="[]"),
        sa.Column("risk_level", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=255), nullable=False, server_default=""),
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
        sa.UniqueConstraint("run_id", name="uq_architecture_critiques_run"),
    )
    op.create_index("ix_architecture_critiques_run_id", "architecture_critiques", ["run_id"])

    # --- follow-up chat thread (twin of cost_summary_messages) ---
    op.create_table(
        "architecture_critique_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "architecture_critique_id",
            UUID(as_uuid=True),
            sa.ForeignKey("architecture_critiques.id", ondelete="CASCADE"),
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
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_architecture_critique_messages_role",
        ),
    )
    op.create_index(
        "ix_architecture_critique_messages_critique_created",
        "architecture_critique_messages",
        ["architecture_critique_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_architecture_critique_messages_critique_created",
        table_name="architecture_critique_messages",
    )
    op.drop_table("architecture_critique_messages")
    op.drop_index("ix_architecture_critiques_run_id", table_name="architecture_critiques")
    op.drop_table("architecture_critiques")
