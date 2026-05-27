"""Add VCS fields to policy_sets.

Revision ID: c7d8e9f0a1b2
Revises: 5a173d4b4e20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "c7d8e9f0a1b2"
down_revision = "5a173d4b4e20"


def upgrade() -> None:
    op.add_column("policy_sets", sa.Column("source", sa.String(20), nullable=False, server_default="inline"))
    op.add_column("policy_sets", sa.Column("vcs_connection_id", UUID(as_uuid=True), nullable=True))
    op.add_column("policy_sets", sa.Column("vcs_repo_url", sa.String(500), nullable=False, server_default=""))
    op.add_column("policy_sets", sa.Column("vcs_branch", sa.String(255), nullable=False, server_default=""))
    op.add_column("policy_sets", sa.Column("policy_path", sa.String(500), nullable=False, server_default=""))
    op.add_column("policy_sets", sa.Column("vcs_last_commit_sha", sa.String(40), nullable=False, server_default=""))
    op.add_column("policy_sets", sa.Column("vcs_last_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("policy_sets", sa.Column("vcs_last_error", sa.String(500), nullable=True))
    op.create_foreign_key(
        "fk_policy_sets_vcs_connection",
        "policy_sets", "vcs_connections",
        ["vcs_connection_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_policy_sets_vcs_connection", "policy_sets", type_="foreignkey")
    op.drop_column("policy_sets", "vcs_last_error")
    op.drop_column("policy_sets", "vcs_last_synced_at")
    op.drop_column("policy_sets", "vcs_last_commit_sha")
    op.drop_column("policy_sets", "policy_path")
    op.drop_column("policy_sets", "vcs_branch")
    op.drop_column("policy_sets", "vcs_repo_url")
    op.drop_column("policy_sets", "vcs_connection_id")
    op.drop_column("policy_sets", "source")
