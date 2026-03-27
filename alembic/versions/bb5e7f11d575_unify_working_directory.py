"""Unify working directory fields.

Migrate vcs_working_directory data into working_directory and drop
vcs_working_directory column. TFE V2 only has working-directory —
the separate vcs_working_directory was non-standard and caused the
field to never reach the runner.

Revision ID: bb5e7f11d575
Revises: 6739dcd36d46
Create Date: 2026-03-27
"""

import sqlalchemy as sa
from alembic import op

revision = "bb5e7f11d575"
down_revision = "6739dcd36d46"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Copy non-empty vcs_working_directory into working_directory
    # where working_directory is currently empty
    op.execute(
        """
        UPDATE workspaces
        SET working_directory = vcs_working_directory
        WHERE (working_directory = '' OR working_directory IS NULL)
          AND vcs_working_directory != ''
          AND vcs_working_directory IS NOT NULL
        """
    )
    op.drop_column("workspaces", "vcs_working_directory")


def downgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "vcs_working_directory",
            sa.String(500),
            nullable=False,
            server_default="",
        ),
    )
