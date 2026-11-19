"""workspaces/rules.storage_location: the named storage location, NULL meaning `default`

Revision ID: 0e1c85ce67d0
Revises: 33575ebac4ef
Create Date: 2026-11-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0e1c85ce67d0"
down_revision = "33575ebac4ef"
branch_labels = None
depends_on = None

_TABLES = ("workspaces", "autodiscovery_rules")


_INDEX = "ix_workspaces_storage_location"


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("storage_location", sa.String(64), nullable=True))
    op.create_index(
        _INDEX,
        "workspaces",
        ["storage_location"],
        postgresql_where=sa.text("storage_location IS NOT NULL"),
    )


def downgrade() -> None:
    # Dropping the column forgets which location holds each workspace's objects.
    named = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM workspaces WHERE storage_location IS NOT NULL"))
        .scalar_one()
    )
    if named:
        raise RuntimeError(
            f"refusing to downgrade: {named} workspace(s) are assigned a storage location, "
            "and their objects could no longer be found"
        )
    op.drop_index(_INDEX, table_name="workspaces")
    for table in _TABLES:
        op.drop_column(table, "storage_location")
