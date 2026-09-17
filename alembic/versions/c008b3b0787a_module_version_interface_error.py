"""Why a registry module version's interface could not be read (#1707).

Pure expand: one nullable text column. Existing rows read as null, meaning "no
recorded failure", which is what every existing row has always implied. An
older replica mid-rolling-upgrade neither reads nor writes it, so a version it
publishes simply has no reason recorded until the next parse.

Revision ID: c008b3b0787a
Revises: b2bece766dc2
"""

import sqlalchemy as sa
from alembic import op

revision = "c008b3b0787a"
down_revision = "b2bece766dc2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "registry_module_versions",
        sa.Column("interface_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("registry_module_versions", "interface_error")
