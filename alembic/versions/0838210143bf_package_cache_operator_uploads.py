"""An operator-uploaded cached artifact records who put it there, and what it replaced

The third posture after verification and scanning (#2589): an operator who has
vetted an artifact with their own process uploads it, and a deployment with the
pull-through sealed serves only what has been vetted that way.

An upload's evidence is not a publisher signature -- the most likely thing an
operator uploads is something they built, patched or re-packaged, which no
upstream key covers. What replaces the signature is **provenance**: a named
person in their own organisation put these exact bytes here, on this date, with
a reference to their own approval record. These columns are that provenance.

`attestation` already distinguishes HOW a row was admitted and gains
`operator-upload` for these, so an uploaded artifact can never read as
fetched-and-verified in the record. These four columns say WHO and WHAT, which
`attestation` alone cannot:

* `uploaded_by` -- the admin principal's email. The record says *uploaded by*,
  always, because a ledger that cannot name the attestor is not the audit trail
  the posture exists for.
* `upload_reference` -- free text, their ticket or approval record. Optional,
  because an operator who does not run a ticketing process is still the
  attestor.
* `replaced_digest` / `replaced_at` -- what was at these coordinates before, and
  when it was displaced. A replacement has to be deliberate (the route refuses
  one that was not asked for) and RECORDED, because the thing being overwritten
  may be what a run already depends on. Without the previous digest that
  information is gone the instant the row is rewritten, which is the silent
  overwrite this exists to prevent.

**Why these columns rather than a table.** The immediately-previous admission is
what an auditor asking "what was here before?" needs, and it belongs on the row
that answers "what is here now". A FULL history is the artifact ledger's job
(#2589), which this column set is the first part of rather than a competitor to.

There is deliberately NO `uploaded_at`: `cached_at` is already when this row's
bytes were written, and for an uploaded row that IS when it was uploaded.
Nothing rewrites it for an artifact row. A second column meaning the same thing
is how two halves of one record come to disagree about a date.

The digest computed on receipt is stored in the existing `digest` column, whose
meaning widens from "upstream's own" to "the digest these bytes were admitted
against, in the form its source expresses it". `attestation` says whose it is, so
no column is needed to tell them apart -- and every surface that already shows a
digest shows the right one without being taught about uploads.

All four are nullable with no server default, which is what makes this
expand-only: a fetched row has no uploader, and NULL is the honest value for it.

Revision ID: 0838210143bf
Revises: 22da47f8fec4
"""

import sqlalchemy as sa
from alembic import op

revision = "0838210143bf"
down_revision = "22da47f8fec4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cached_package_files",
        sa.Column("uploaded_by", sa.String(length=320), nullable=True),
    )
    op.add_column(
        "cached_package_files",
        sa.Column("upload_reference", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "cached_package_files",
        sa.Column("replaced_digest", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "cached_package_files",
        sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cached_package_files", "replaced_at")
    op.drop_column("cached_package_files", "replaced_digest")
    op.drop_column("cached_package_files", "upload_reference")
    op.drop_column("cached_package_files", "uploaded_by")
