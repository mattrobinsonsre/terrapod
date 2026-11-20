"""Widen artifact_ledger.digest: an npm SRI is 95 characters, the column was 80

The ledger is keyed on the digest, and npm's digest is the SRI string upstream
publishes -- `sha512-` plus 88 base64 characters, so 95. The column was
`varchar(80)`, which made every npm admission raise `22001
string_data_right_truncation`.

`record_admission` catches and logs that, so nothing surfaced -- but the
transaction is already aborted by then, so the `CachedPackageFile` row written
moments earlier never committed either. npm pull-through therefore cached
NOTHING: every install re-fetched upstream, a sealed node could not serve npm at
all, and no ledger row existed for any npm artifact, which is the audit surface
the ledger was built to produce.

200 to match `cached_package_files.digest`, the other column holding a digest
in whatever shape upstream published it. Every other digest column in the tree
is 140 or 200; the 80 was the outlier.

Expand-only: widening a varchar rewrites no rows and an older replica keeps
reading it, so this is safe mid-rolling-upgrade.

Revision ID: 24a459aa2107
Revises: 0e1c85ce67d0
"""

import sqlalchemy as sa
from alembic import op

revision = "24a459aa2107"
down_revision = "0e1c85ce67d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "artifact_ledger",
        "digest",
        existing_type=sa.String(length=80),
        type_=sa.String(length=200),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Narrowing can only succeed where nothing longer than 80 was ever stored,
    # which is exactly the state this migration exists to leave behind. It is
    # offered so the revision is reversible on a database that never admitted an
    # npm artifact; on one that did, Postgres refuses and that refusal is
    # honest -- there is no lossless way back.
    op.alter_column(
        "artifact_ledger",
        "digest",
        existing_type=sa.String(length=200),
        type_=sa.String(length=80),
        existing_nullable=False,
    )
