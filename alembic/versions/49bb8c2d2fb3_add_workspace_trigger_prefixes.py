"""Add workspace trigger_prefixes column.

Revision ID: 49bb8c2d2fb3
Revises: bb5e7f11d575
Create Date: 2026-03-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "49bb8c2d2fb3"
down_revision = "bb5e7f11d575"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "trigger_prefixes",
            ARRAY(sa.String(255)),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "trigger_prefixes")
