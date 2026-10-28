"""Allow catalog items to designate a self-contained root module (#1990).

Revision ID: 700a8913af99
Revises: aac35aebdea1
"""

import sqlalchemy as sa

from alembic import op

revision: str = "700a8913af99"
down_revision: str | None = "aac35aebdea1"


def upgrade() -> None:
    op.add_column(
        "catalog_items",
        sa.Column("self_contained", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("catalog_items", "self_contained")
