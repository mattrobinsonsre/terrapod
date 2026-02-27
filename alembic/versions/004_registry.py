"""Private module and provider registry tables.

Adds tables for the private module/provider registry:
- registry_modules: top-level module entities
- registry_module_versions: semver versions with upload tracking
- registry_providers: top-level provider entities
- registry_provider_versions: versions with GPG key ref
- registry_provider_platforms: per-OS/arch binaries
- gpg_keys: GPG public keys for provider signing

Revision ID: 004
Revises: 003
Create Date: 2026-02-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- GPG Keys (must be created before provider_versions which references it) ---
    op.create_table(
        "gpg_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_name", sa.String(63), nullable=False),
        sa.Column("key_id", sa.String(40), nullable=False),
        sa.Column("ascii_armor", sa.Text(), nullable=False),
        sa.Column("source", sa.String(63), nullable=False, server_default="terrapod"),
        sa.Column("source_url", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("org_name", "key_id", name="uq_gpg_keys"),
    )

    # --- Registry Modules ---
    op.create_table(
        "registry_modules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_name", sa.String(63), nullable=False),
        sa.Column("namespace", sa.String(63), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("provider", sa.String(63), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "org_name", "namespace", "name", "provider", name="uq_registry_modules"
        ),
    )

    op.create_table(
        "registry_module_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "module_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("registry_modules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.String(63), nullable=False),
        sa.Column(
            "upload_status", sa.String(20), nullable=False, server_default="pending"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("module_id", "version", name="uq_registry_module_versions"),
    )
    op.create_index(
        "ix_registry_module_versions_module_id",
        "registry_module_versions",
        ["module_id"],
    )

    # --- Registry Providers ---
    op.create_table(
        "registry_providers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_name", sa.String(63), nullable=False),
        sa.Column("namespace", sa.String(63), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "org_name", "namespace", "name", name="uq_registry_providers"
        ),
    )

    op.create_table(
        "registry_provider_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "provider_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("registry_providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.String(63), nullable=False),
        sa.Column(
            "gpg_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("gpg_keys.id"),
            nullable=True,
        ),
        sa.Column(
            "protocols",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[\"5.0\"]'::jsonb"),
        ),
        sa.Column(
            "shasums_uploaded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "shasums_sig_uploaded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "provider_id", "version", name="uq_registry_provider_versions"
        ),
    )
    op.create_index(
        "ix_registry_provider_versions_provider_id",
        "registry_provider_versions",
        ["provider_id"],
    )

    op.create_table(
        "registry_provider_platforms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("registry_provider_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("os", sa.String(20), nullable=False),
        sa.Column("arch", sa.String(20), nullable=False),
        sa.Column("shasum", sa.String(64), nullable=False, server_default=""),
        sa.Column("filename", sa.String(255), nullable=False, server_default=""),
        sa.Column(
            "upload_status", sa.String(20), nullable=False, server_default="pending"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "version_id", "os", "arch", name="uq_registry_provider_platforms"
        ),
    )
    op.create_index(
        "ix_registry_provider_platforms_version_id",
        "registry_provider_platforms",
        ["version_id"],
    )


def downgrade() -> None:
    op.drop_table("registry_provider_platforms")
    op.drop_table("registry_provider_versions")
    op.drop_table("registry_providers")
    op.drop_table("registry_module_versions")
    op.drop_table("registry_modules")
    op.drop_table("gpg_keys")
