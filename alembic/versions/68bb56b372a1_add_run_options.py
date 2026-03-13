"""Add run customization options (target, replace, refresh-only, refresh, allow-empty-apply).

Revision ID: 68bb56b372a1
Revises: fe70f352330a
Create Date: 2026-03-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

revision = "68bb56b372a1"
down_revision = "fe70f352330a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("target_addrs", ARRAY(sa.Text), nullable=True))
    op.add_column("runs", sa.Column("replace_addrs", ARRAY(sa.Text), nullable=True))
    op.add_column(
        "runs",
        sa.Column("refresh_only", sa.Boolean, nullable=False, server_default="false"),
    )
    op.add_column(
        "runs",
        sa.Column("refresh", sa.Boolean, nullable=False, server_default="true"),
    )
    op.add_column(
        "runs",
        sa.Column(
            "allow_empty_apply", sa.Boolean, nullable=False, server_default="false"
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "allow_empty_apply")
    op.drop_column("runs", "refresh")
    op.drop_column("runs", "refresh_only")
    op.drop_column("runs", "replace_addrs")
    op.drop_column("runs", "target_addrs")
