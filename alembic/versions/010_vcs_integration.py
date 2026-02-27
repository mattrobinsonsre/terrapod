"""VCS integration: connections, workspace VCS columns, run VCS metadata.

Adds the vcs_connections table (supports GitHub, GitLab) and VCS-related
columns to workspaces and runs for polling-first, webhooks-optional design.

Revision ID: 010
Revises: 009
Create Date: 2026-02-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- vcs_connections table ---
    op.create_table(
        "vcs_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_name", sa.String(63), nullable=False, server_default="default"),
        sa.Column("provider", sa.String(20), nullable=False, server_default="github"),
        sa.Column("name", sa.String(255), nullable=False),
        # Provider-agnostic
        sa.Column("server_url", sa.String(500), nullable=False, server_default=""),
        sa.Column("token_encrypted", sa.Text, nullable=True),
        # GitHub-specific
        sa.Column("github_app_id", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "github_installation_id", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "github_account_login", sa.String(255), nullable=False, server_default=""
        ),
        sa.Column(
            "github_account_type", sa.String(20), nullable=False, server_default=""
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "provider", "github_installation_id", name="uq_vcs_connections_install"
        ),
    )

    # --- Workspace VCS columns ---
    op.add_column(
        "workspaces",
        sa.Column(
            "vcs_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vcs_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("vcs_repo_url", sa.String(500), nullable=False, server_default=""),
    )
    op.add_column(
        "workspaces",
        sa.Column("vcs_branch", sa.String(255), nullable=False, server_default=""),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "vcs_working_directory", sa.String(500), nullable=False, server_default=""
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "vcs_last_commit_sha", sa.String(40), nullable=False, server_default=""
        ),
    )

    # --- Run VCS metadata columns ---
    op.add_column(
        "runs",
        sa.Column("vcs_commit_sha", sa.String(40), nullable=False, server_default=""),
    )
    op.add_column(
        "runs",
        sa.Column("vcs_branch", sa.String(255), nullable=False, server_default=""),
    )
    op.add_column(
        "runs",
        sa.Column("vcs_pull_request_number", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    # --- Run VCS columns ---
    op.drop_column("runs", "vcs_pull_request_number")
    op.drop_column("runs", "vcs_branch")
    op.drop_column("runs", "vcs_commit_sha")

    # --- Workspace VCS columns ---
    op.drop_column("workspaces", "vcs_last_commit_sha")
    op.drop_column("workspaces", "vcs_working_directory")
    op.drop_column("workspaces", "vcs_branch")
    op.drop_column("workspaces", "vcs_repo_url")
    op.drop_column("workspaces", "vcs_connection_id")

    # --- vcs_connections table ---
    op.drop_table("vcs_connections")
