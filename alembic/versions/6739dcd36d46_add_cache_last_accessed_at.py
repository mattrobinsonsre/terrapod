"""Add last_accessed_at to cache tables for access-based retention.

Revision ID: 6739dcd36d46
Revises: 00a2cf7e0345
Create Date: 2026-03-25
"""

import sqlalchemy as sa
from alembic import op

revision = "6739dcd36d46"
down_revision = "00a2cf7e0345"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cached_binaries",
        sa.Column(
            "last_accessed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column(
        "cached_provider_packages",
        sa.Column(
            "last_accessed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Backfill: set last_accessed_at = cached_at for existing rows
    op.execute("UPDATE cached_binaries SET last_accessed_at = cached_at")
    op.execute("UPDATE cached_provider_packages SET last_accessed_at = cached_at")


def downgrade() -> None:
    op.drop_column("cached_provider_packages", "last_accessed_at")
    op.drop_column("cached_binaries", "last_accessed_at")
