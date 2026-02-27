"""Provider and binary caching tables.

Adds tables for the pull-through caching layer:
- cached_provider_packages: cached upstream provider binaries
- cached_binaries: cached terraform/tofu CLI binaries

Revision ID: 005
Revises: 004
Create Date: 2026-02-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cached_provider_packages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("namespace", sa.String(63), nullable=False),
        sa.Column("type", sa.String(63), nullable=False),
        sa.Column("version", sa.String(63), nullable=False),
        sa.Column("os", sa.String(20), nullable=False),
        sa.Column("arch", sa.String(20), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("shasum", sa.String(64), nullable=False),
        sa.Column("h1_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "cached_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "hostname",
            "namespace",
            "type",
            "version",
            "os",
            "arch",
            name="uq_cached_provider_packages",
        ),
    )
    op.create_index(
        "ix_cached_provider_packages_lookup",
        "cached_provider_packages",
        ["hostname", "namespace", "type"],
    )

    op.create_table(
        "cached_binaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tool", sa.String(20), nullable=False),
        sa.Column("version", sa.String(63), nullable=False),
        sa.Column("os", sa.String(20), nullable=False),
        sa.Column("arch", sa.String(20), nullable=False),
        sa.Column("shasum", sa.String(64), nullable=False, server_default=""),
        sa.Column("download_url", sa.String(1000), nullable=False, server_default=""),
        sa.Column(
            "cached_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tool", "version", "os", "arch", name="uq_cached_binaries"),
    )


def downgrade() -> None:
    op.drop_table("cached_binaries")
    op.drop_table("cached_provider_packages")
