"""pr_sessions.merge_commit_sha — tie a post-merge run back to its PR (#1878)

Expand-only: one nullable column, no backfill. Sessions that predate it simply
have no merge commit recorded, so the status comment leaves their rows exactly
as it does today rather than reporting something it cannot know.

Revision ID: 58c3a8aca3a5
Revises: e3f76c455ce6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "58c3a8aca3a5"
down_revision: str | None = "e3f76c455ce6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pr_sessions",
        sa.Column("merge_commit_sha", sa.String(length=40), nullable=True),
    )
    # The lookup this exists for is "which session merged as this commit?",
    # asked once per new tracked-branch commit on a repo Terrapod polls.
    op.create_index(
        "ix_pr_sessions_merge_commit_sha",
        "pr_sessions",
        ["vcs_connection_id", "merge_commit_sha"],
    )


def downgrade() -> None:
    op.drop_index("ix_pr_sessions_merge_commit_sha", table_name="pr_sessions")
    op.drop_column("pr_sessions", "merge_commit_sha")
