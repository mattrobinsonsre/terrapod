"""Registry upload improvements: VCS source columns for modules.

Adds VCS source tracking to registry_modules for auto-publish on tag push.

Revision ID: 851c0ca75afe
Revises: fe05e9430d5a
Create Date: 2026-03-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "851c0ca75afe"
down_revision: Union[str, None] = "fe05e9430d5a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Module VCS source columns ──────────────────────────────────
    op.add_column(
        "registry_modules",
        sa.Column("source", sa.String(20), nullable=False, server_default="upload"),
    )
    op.add_column(
        "registry_modules",
        sa.Column(
            "vcs_connection_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vcs_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "registry_modules",
        sa.Column("vcs_repo_url", sa.String(500), nullable=False, server_default=""),
    )
    op.add_column(
        "registry_modules",
        sa.Column("vcs_branch", sa.String(255), nullable=False, server_default=""),
    )
    op.add_column(
        "registry_modules",
        sa.Column("vcs_tag_pattern", sa.String(255), nullable=False, server_default="v*"),
    )
    op.add_column(
        "registry_modules",
        sa.Column("vcs_last_tag", sa.String(255), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("registry_modules", "vcs_last_tag")
    op.drop_column("registry_modules", "vcs_tag_pattern")
    op.drop_column("registry_modules", "vcs_branch")
    op.drop_column("registry_modules", "vcs_repo_url")
    op.drop_column("registry_modules", "vcs_connection_id")
    op.drop_column("registry_modules", "source")
