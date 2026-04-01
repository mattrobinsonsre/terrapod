"""add vcs error tracking

Revision ID: 3efe17078298
Revises: c5734d5c739d
Create Date: 2026-04-01
"""

import sqlalchemy as sa
from alembic import op

revision = "3efe17078298"
down_revision = "c5734d5c739d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("vcs_last_polled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("workspaces", sa.Column("vcs_last_error", sa.String(500), nullable=True))
    op.add_column(
        "workspaces",
        sa.Column("vcs_last_error_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "vcs_last_error_at")
    op.drop_column("workspaces", "vcs_last_error")
    op.drop_column("workspaces", "vcs_last_polled_at")
