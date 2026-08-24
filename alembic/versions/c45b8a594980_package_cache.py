"""Cached artifacts from language package registries (#1417).

Pure expand: one new table, nothing existing touched, so a rolling upgrade runs
old and new replicas against it safely.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "c45b8a594980"
down_revision = "cddfa0bf6bfb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cached_package_files",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("ecosystem", sa.String(20), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("digest", sa.String(200), nullable=False, server_default=""),
        sa.Column("cached_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("ecosystem", "name", "filename", name="uq_cached_package_files"),
    )
    op.create_index("ix_cached_package_files_lookup", "cached_package_files", ["ecosystem", "name"])
    # Retention sweeps by last access, so that column is the one that gets scanned.
    op.create_index(
        "ix_cached_package_files_accessed", "cached_package_files", ["last_accessed_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_cached_package_files_accessed", table_name="cached_package_files")
    op.drop_index("ix_cached_package_files_lookup", table_name="cached_package_files")
    op.drop_table("cached_package_files")
