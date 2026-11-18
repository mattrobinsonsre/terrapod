"""Record where a cached binary or provider object is, rather than deriving it

An operator-uploaded artifact cannot be re-fetched from anywhere, where
pull-through content merely goes cold -- so the two differ in the one way
replication cares about, and `blob_classes` can only tell them apart by the
storage key: `owning_class` resolves exactly one class per key by longest prefix
match, deliberately, because overlapping classes would be "counted twice in a
readiness report, copied twice by the copier". Two classes cannot share
`cache/binaries/`.

`cached_package_files` already records its key, which is why uploads there can
move under `uploads/` with no schema change at all. These two tables derived
theirs from their coordinates at roughly eight call sites across five modules, so
an upload had nowhere to say it lives somewhere else.

Expand-only and reversible. The column is nullable and backfilled with the key
each row's coordinates already derive, so after this migration every existing row
says exactly where its object has always been and nothing moves. Readers go
through `storage_key_of`, which falls back to the derived key while the column is
NULL -- so an old replica serving traffic during a rolling upgrade is unaffected,
and the column stops being load-bearing only when something is written under a
prefix the coordinates do not describe.

No `schema_contractions` row: this adds a column and removes nothing, so an older
pod reading this table is fine.

Revision ID: 667fd9da2621
Revises: a38b26d03bd2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "667fd9da2621"
down_revision: str | None = "a38b26d03bd2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cached_binaries",
        sa.Column("storage_key", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "cached_provider_packages",
        sa.Column("storage_key", sa.String(length=1024), nullable=True),
    )

    # Backfill with the key the coordinates already derive, so the column is a
    # statement of where the object IS rather than a promise about future rows.
    # The expressions mirror `keys.binary_cache_key` and `keys.provider_cache_key`
    # exactly; `test_storage_key_backfill_matches_the_key_helpers` compares them
    # against those helpers so a divergence fails rather than silently pointing a
    # row at nothing.
    op.execute(
        """
        UPDATE cached_binaries
           SET storage_key = 'cache/binaries/' || tool || '/' || version || '/'
                             || os || '_' || arch
         WHERE storage_key IS NULL
        """
    )
    op.execute(
        """
        UPDATE cached_provider_packages
           SET storage_key = 'cache/providers/' || hostname || '/' || namespace
                             || '/' || type || '/' || version || '/' || filename
         WHERE storage_key IS NULL
        """
    )


def downgrade() -> None:
    # Dropping the column loses only where an object is, and for every row whose
    # object sits at its derived key that is recoverable by deriving it again --
    # which is what `storage_key_of` does when the column is NULL.
    #
    # It is NOT recoverable for a row written under `uploads/`, so this downgrade
    # is lossy in exactly one direction: an operator who uploaded an artifact
    # after upgrading, then downgraded, has a row whose object the old code cannot
    # find. That is stated rather than silently accepted, and it is why the column
    # is the mechanism rather than a convention about prefixes.
    op.drop_column("cached_provider_packages", "storage_key")
    op.drop_column("cached_binaries", "storage_key")
