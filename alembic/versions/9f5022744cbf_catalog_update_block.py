"""Record blocked self-contained catalog publishes (#1990).

Revision ID: 9f5022744cbf
Revises: b2455a23938c
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9f5022744cbf"
down_revision: str | None = "b2455a23938c"


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("catalog_update_block", postgresql.JSONB()))


def downgrade() -> None:
    op.drop_column("workspaces", "catalog_update_block")
