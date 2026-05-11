"""add apply-then-merge schema (#282)

Phase 1 of apply-then-merge: adds the new workspace columns
(`vcs_workflow`, `auto_merge`, `auto_merge_strategy`), the new nullable
run columns (`vcs_apply_blocked_reason`, `vcs_actor_login`,
`vcs_actor_user_id`), and the `pr_sessions` table that tracks the
edit-in-place status comment + poll cursors for each open PR.

No behavioural change — purely the data model for subsequent phases.

Revision ID: c17aecf92ac8
Revises: a10f40e65a96
Create Date: 2026-05-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "c17aecf92ac8"
down_revision: str | None = "a10f40e65a96"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Workspace: VCS workflow mode + auto-merge config.
    op.add_column(
        "workspaces",
        sa.Column(
            "vcs_workflow",
            sa.String(20),
            nullable=False,
            server_default="merge_then_apply",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("auto_merge", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "auto_merge_strategy",
            sa.String(20),
            nullable=False,
            server_default="merge",
        ),
    )

    # Run: apply-blocked reason + VCS actor for comment-driven actions.
    op.add_column(
        "runs",
        sa.Column("vcs_apply_blocked_reason", sa.String(500), nullable=True),
    )
    op.add_column("runs", sa.Column("vcs_actor_login", sa.String(255), nullable=True))
    op.add_column("runs", sa.Column("vcs_actor_user_id", sa.String(64), nullable=True))

    # PR conversation state (status comment + poll cursors + lifecycle).
    op.create_table(
        "pr_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vcs_connection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("vcs_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("repo", sa.String(500), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(40), nullable=False, server_default=""),
        sa.Column("status_comment_id", sa.String(64), nullable=True),
        sa.Column("last_processed_comment_id", sa.String(64), nullable=True),
        sa.Column("last_processed_review_id", sa.String(64), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="open"),
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
        sa.UniqueConstraint("vcs_connection_id", "repo", "pr_number", name="uq_pr_session"),
    )
    op.create_index("ix_pr_sessions_open", "pr_sessions", ["vcs_connection_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_pr_sessions_open", table_name="pr_sessions")
    op.drop_table("pr_sessions")
    op.drop_column("runs", "vcs_actor_user_id")
    op.drop_column("runs", "vcs_actor_login")
    op.drop_column("runs", "vcs_apply_blocked_reason")
    op.drop_column("workspaces", "auto_merge_strategy")
    op.drop_column("workspaces", "auto_merge")
    op.drop_column("workspaces", "vcs_workflow")
