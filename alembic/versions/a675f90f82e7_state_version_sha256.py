"""Add sha256 to state_versions for collision-resistant divergence checks.

The state-divergence equality check (does a re-uploaded state at an existing
serial match the recorded one, or is it a genuine conflict?) previously
compared md5. An md5 collision could make two distinct states compare equal
and suppress a real divergence flag. Add a Terrapod-internal sha256 column
(md5 stays for the go-tfe state-version contract) and use it for the compare,
falling back to md5 for legacy rows written before this column existed.

Revision ID: a675f90f82e7
Revises: 1c014bf5ae6d
"""

import sqlalchemy as sa

from alembic import op

revision = "a675f90f82e7"
down_revision = "1c014bf5ae6d"


def upgrade() -> None:
    op.add_column(
        "state_versions",
        sa.Column("sha256", sa.String(64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("state_versions", "sha256")
