"""A registry for published Ansible collections (#1482).

Published collections deliberately get their own tables rather than living in
`cached_package_files`. A cached artifact is a copy of something upstream still
holds, so the retention sweep may evict it by `last_accessed_at` and refetch on
demand; a published collection is the only copy there is, and reaping it thirty
days after someone last installed it would be silent data loss. These sit
alongside the module and provider registries, which are governed the same way.

Purely additive: two new tables, nothing existing is touched.

Revision ID: edd2bcb183de
Revises: 4408d25e7130
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "edd2bcb183de"
down_revision = "4408d25e7130"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "registry_collections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("namespace", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column(
            "labels",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("owner_email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("namespace", "name", name="uq_registry_collections"),
    )

    op.create_table(
        "registry_collection_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "collection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("registry_collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.String(length=63), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("signature", sa.Text(), nullable=False, server_default=""),
        sa.Column("signing_key_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("collection_id", "version", name="uq_registry_collection_versions"),
    )


def downgrade() -> None:
    op.drop_table("registry_collection_versions")
    op.drop_table("registry_collections")
