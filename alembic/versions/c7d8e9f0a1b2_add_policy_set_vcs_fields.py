"""Add VCS fields to policy_sets.

Revision ID: c7d8e9f0a1b2
Revises: 6e2f3a8b9c10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "c7d8e9f0a1b2"
down_revision = "6e2f3a8b9c10"


def upgrade() -> None:
    op.add_column(
        "policy_sets",
        sa.Column("source", sa.String(20), nullable=False, server_default="inline"),
    )
    op.add_column("policy_sets", sa.Column("vcs_connection_id", UUID(as_uuid=True), nullable=True))
    op.add_column(
        "policy_sets",
        sa.Column("vcs_repo_url", sa.String(500), nullable=False, server_default=""),
    )
    op.add_column(
        "policy_sets",
        sa.Column("vcs_branch", sa.String(255), nullable=False, server_default=""),
    )
    op.add_column(
        "policy_sets",
        sa.Column("policy_path", sa.String(500), nullable=False, server_default=""),
    )
    op.add_column(
        "policy_sets",
        sa.Column("vcs_last_commit_sha", sa.String(40), nullable=False, server_default=""),
    )
    op.add_column(
        "policy_sets",
        sa.Column("vcs_last_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("policy_sets", sa.Column("vcs_last_error", sa.String(500), nullable=True))
    op.create_foreign_key(
        "fk_policy_sets_vcs_connection",
        "policy_sets",
        "vcs_connections",
        ["vcs_connection_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_policy_sets_source",
        "policy_sets",
        "source IN ('inline', 'vcs')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_policy_sets_source", "policy_sets", type_="check")
    op.drop_constraint("fk_policy_sets_vcs_connection", "policy_sets", type_="foreignkey")
    op.drop_column("policy_sets", "vcs_last_error")
    op.drop_column("policy_sets", "vcs_last_synced_at")
    op.drop_column("policy_sets", "vcs_last_commit_sha")
    op.drop_column("policy_sets", "policy_path")
    op.drop_column("policy_sets", "vcs_branch")
    op.drop_column("policy_sets", "vcs_repo_url")
    op.drop_column("policy_sets", "vcs_connection_id")
    op.drop_column("policy_sets", "source")
