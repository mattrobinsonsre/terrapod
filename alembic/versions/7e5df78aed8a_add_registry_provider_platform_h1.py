"""Add h1_hash column to registry_provider_platforms.

The mirror ('/v1/providers/.../{version}.json') gains a Tier-0 lookup
that serves self-hosted providers from the registry tables. Like the
upstream cache (cached_provider_packages.h1_hash), we persist the
terraform/tofu h1 dirhash so the runner's lock-extender can splice it
into .terraform.lock.hcl without falling back to `tofu providers lock`.

h1 is computed eagerly at upload-confirm time and lazy-backfilled on
first read if it's still empty (e.g. for rows from before this column
existed, or where the eager compute failed). Best-effort — empty h1
just degrades to the prior fall-back behaviour.

Revision ID: 7e5df78aed8a
Revises: 4541c1ddfdb7
"""

import sqlalchemy as sa
from alembic import op

revision = "7e5df78aed8a"
down_revision = "4541c1ddfdb7"


def upgrade() -> None:
    op.add_column(
        "registry_provider_platforms",
        sa.Column(
            "h1_hash",
            sa.String(64),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("registry_provider_platforms", "h1_hash")
