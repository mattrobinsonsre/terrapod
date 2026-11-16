"""Record how a cached package artifact was admitted, and re-fetch the ones we cannot vouch for

Every row in `cached_package_files` predating digest verification was admitted
without one, and a cache hit is served without re-checking -- so without this
those artifacts would be served unverified for ever, while everything fetched
afterwards is verified. An operator could not tell the two populations apart, and
neither could we.

`attestation` is how they are told apart. NULL means "admitted by a build that
recorded nothing", which `substrate.lookup_present` treats as a miss: the artifact
is re-fetched once, verified under the current rules, and the row is rewritten with
what was actually checked. Re-verification is therefore lazy and per artifact, on
next use, rather than a purge.

**Why not delete the rows instead.** Deleting them is the obvious form and it is
worse three ways. It orphans every object in the store -- a re-fetch overwrites the
same key, but an artifact never requested again is unreachable from any row and
retention works from rows, so it would leak for ever. It empties the cache in one
step rather than refilling it as things are used, which on a large estate is a
thundering herd at exactly the moment after an upgrade. And a deletion has no
inverse, so `downgrade()` could only be a stub, which this repository's migration
contract refuses on purpose.

This column is also the first field of the artifact ledger (#2589) rather than a
throwaway: the values are the vocabulary that ledger uses, so the next change
extends it rather than replacing it.

**The one consequence worth stating loudly**, because it is an outage for one
posture: a NULL row is a miss, and under `registry.cache_only` a miss cannot be
filled. A sealed deployment therefore cannot serve a legacy artifact until it has
been re-fetched with egress, which makes its upgrade unseal -> re-warm -> re-seal.
See `docs/upgrading-to-2.0.md`.

Revision ID: 22da47f8fec4
Revises: 069d08e4effa
"""

import sqlalchemy as sa
from alembic import op

revision = "22da47f8fec4"
down_revision = "069d08e4effa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no server default, deliberately: every existing row must come
    # out NULL, because NULL is the whole signal. A default would mark artifacts
    # admitted before verification existed as though they had been checked, which
    # is the one thing this column exists to prevent.
    op.add_column(
        "cached_package_files",
        sa.Column("attestation", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cached_package_files", "attestation")
