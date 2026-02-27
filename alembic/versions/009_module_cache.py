"""Module caching table.

Adds the cached_modules table for pull-through module caching.

Revision ID: 009
Revises: 008
Create Date: 2026-02-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cached_modules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("namespace", sa.String(63), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("provider", sa.String(63), nullable=False),
        sa.Column("version", sa.String(63), nullable=False),
        sa.Column("shasum", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "cached_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "hostname",
            "namespace",
            "name",
            "provider",
            "version",
            name="uq_cached_modules",
        ),
    )
    op.create_index(
        "ix_cached_modules_lookup",
        "cached_modules",
        ["hostname", "namespace", "name", "provider"],
    )


def downgrade() -> None:
    op.drop_table("cached_modules")
