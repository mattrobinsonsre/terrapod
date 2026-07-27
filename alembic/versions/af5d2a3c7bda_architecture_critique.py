"""architecture critique tables (#1036 Part 2 / #963)

State-based, whole-system AI architecture critic: infers a workspace's
architecture from its current Terraform state (+ graph, cost, security
findings) and critiques it. One critique row per (workspace, state_version),
plus a chat-follow-up messages table. Purely additive.

Revision ID: af5d2a3c7bda
Revises: 10e9f2f624ea
Create Date: 2026-07-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "af5d2a3c7bda"
down_revision: str | None = "10e9f2f624ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "architecture_critiques",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state_serial", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column(
            "architecture",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("risk_level", sa.String(length=20), server_default="", nullable=False),
        sa.Column(
            "findings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "deferred",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=255), server_default="", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["state_version_id"], ["state_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state_version_id", name="uq_architecture_critiques_state_version"),
    )
    op.create_index(
        "ix_architecture_critiques_workspace_created",
        "architecture_critiques",
        ["workspace_id", "created_at"],
    )

    op.create_table(
        "architecture_critique_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("critique_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("model", sa.String(length=255), server_default="", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["critique_id"], ["architecture_critiques.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_architecture_critique_messages_role",
        ),
    )
    op.create_index(
        "ix_architecture_critique_messages_critique_created",
        "architecture_critique_messages",
        ["critique_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_architecture_critique_messages_critique_created",
        table_name="architecture_critique_messages",
    )
    op.drop_table("architecture_critique_messages")
    op.drop_index(
        "ix_architecture_critiques_workspace_created",
        table_name="architecture_critiques",
    )
    op.drop_table("architecture_critiques")
