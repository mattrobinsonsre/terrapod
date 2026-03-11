"""Add workspace var_files column.

Revision ID: b43b10a4da62
Revises: 1253815f7106
Create Date: 2026-03-11
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

revision = "b43b10a4da62"
down_revision = "1253815f7106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("var_files", ARRAY(sa.String(500)), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "var_files")
