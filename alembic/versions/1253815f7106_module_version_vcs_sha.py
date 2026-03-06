"""Add vcs_commit_sha and vcs_tag to registry_module_versions.

Track which git commit and tag produced each VCS-published module version.
Enables re-downloading archives when a tag is moved to a new commit.

Revision ID: 1253815f7106
Revises: 2db753c16b9d
Create Date: 2026-03-06
"""

from alembic import op
import sqlalchemy as sa

revision = "1253815f7106"
down_revision = "2db753c16b9d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "registry_module_versions",
        sa.Column("vcs_commit_sha", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column(
        "registry_module_versions",
        sa.Column("vcs_tag", sa.String(255), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("registry_module_versions", "vcs_tag")
    op.drop_column("registry_module_versions", "vcs_commit_sha")
