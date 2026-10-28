"""Catalog execution-backend version constraints (#1990).

Revision ID: b2455a23938c
Revises: 700a8913af99
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b2455a23938c"
down_revision: str | None = "700a8913af99"


def upgrade() -> None:
    op.add_column("catalog_items", sa.Column("execution_backend_constraints", postgresql.JSONB()))


def downgrade() -> None:
    op.drop_column("catalog_items", "execution_backend_constraints")
