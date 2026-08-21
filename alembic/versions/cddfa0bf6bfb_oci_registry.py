"""OCI Distribution registry tables (#1408)

Pure expand: six new tables, nothing altered and nothing dropped, so a lagging
replica during a rolling upgrade neither sees nor needs them.

Revision ID: cddfa0bf6bfb
Revises: e1bf96919d09
Create Date: 2026-08-21

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "cddfa0bf6bfb"
down_revision = "e1bf96919d09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oci_repositories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # 255 is the distribution spec's limit on a repository name.
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        # NULL means locally pushed; a hostname means a pull-through mirror.
        # The two differ in lifecycle, so the distinction has to be stored.
        sa.Column("upstream", sa.String(255), nullable=True),
        sa.Column("labels", JSONB, nullable=False, server_default="{}"),
        sa.Column("owner_email", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_oci_repositories_upstream", "oci_repositories", ["upstream"])

    op.create_table(
        "oci_blobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # algorithm:hex — 140 accommodates sha512 (7 + 128) with headroom.
        sa.Column("digest", sa.String(140), nullable=False, unique=True),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=False),
    )

    # The link table: blobs are global and content-addressed, so this is what
    # decides which repository may serve which blob, what a cross-repository
    # mount writes instead of re-uploading, and the edge set a future
    # reference-walk GC traverses.
    op.create_table(
        "oci_repository_blobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repository_id",
            UUID(as_uuid=True),
            sa.ForeignKey("oci_repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "blob_id",
            UUID(as_uuid=True),
            sa.ForeignKey("oci_blobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repository_id", "blob_id", name="uq_oci_repo_blob"),
    )
    # Indexed from the blob side too: GC and "who else references this" both
    # traverse the edge in that direction, which the unique constraint's
    # composite index does not serve.
    op.create_index("ix_oci_repository_blobs_blob_id", "oci_repository_blobs", ["blob_id"])

    op.create_table(
        "oci_manifests",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repository_id",
            UUID(as_uuid=True),
            sa.ForeignKey("oci_repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("digest", sa.String(140), nullable=False),
        sa.Column("media_type", sa.String(140), nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=False),
        # Referrers metadata, denormalised out of the manifest body: the
        # referrers API queries by subject, and parsing every manifest in a
        # repository to answer one lookup does not scale.
        sa.Column("subject_digest", sa.String(140), nullable=True),
        sa.Column("artifact_type", sa.String(140), nullable=True),
        sa.Column("annotations", JSONB, nullable=True),
        sa.UniqueConstraint("repository_id", "digest", name="uq_oci_manifest_repo_digest"),
    )
    op.create_index(
        "ix_oci_manifests_subject", "oci_manifests", ["repository_id", "subject_digest"]
    )

    op.create_table(
        "oci_tags",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repository_id",
            UUID(as_uuid=True),
            sa.ForeignKey("oci_repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The spec caps tags at 128 characters.
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column(
            "manifest_id",
            UUID(as_uuid=True),
            sa.ForeignKey("oci_manifests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repository_id", "name", name="uq_oci_tag_repo_name"),
    )

    # Upload state in Postgres, not memory: the API is multi-replica with no
    # session affinity, so POST opens the session on one replica and each PATCH
    # may land on another.
    op.create_table(
        "oci_upload_sessions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("repository_name", sa.String(255), nullable=False),
        sa.Column("offset", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Reaping abandoned sessions scans by age, so it is indexed.
    op.create_index("ix_oci_upload_sessions_updated_at", "oci_upload_sessions", ["updated_at"])


def downgrade() -> None:
    # Reverse creation order so the foreign keys unwind cleanly.
    op.drop_index("ix_oci_upload_sessions_updated_at", table_name="oci_upload_sessions")
    op.drop_table("oci_upload_sessions")
    op.drop_table("oci_tags")
    op.drop_index("ix_oci_manifests_subject", table_name="oci_manifests")
    op.drop_table("oci_manifests")
    op.drop_index("ix_oci_repository_blobs_blob_id", table_name="oci_repository_blobs")
    op.drop_table("oci_repository_blobs")
    op.drop_table("oci_blobs")
    op.drop_index("ix_oci_repositories_upstream", table_name="oci_repositories")
    op.drop_table("oci_repositories")
