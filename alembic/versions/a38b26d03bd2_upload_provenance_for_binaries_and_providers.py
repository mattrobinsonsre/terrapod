"""Provenance columns on the binary and provider caches (#2599).

The package cache already records who uploaded an artifact, when, against what
reference, and what a replacement displaced. The binary and provider caches
could not record any of it, so the two artifact classes a Terraform run cannot
proceed without -- the engine itself and its providers -- were the two an
operator could not supply to a sealed deployment with an audit trail.

Expand-only: every column is nullable with no backfill, so an older replica
reading these tables during a rolling upgrade is unaffected. NULL means "not an
upload", which is what every existing row is.

Revision ID: a38b26d03bd2
Revises: 0838210143bf
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a38b26d03bd2"
down_revision = "0838210143bf"
branch_labels = None
depends_on = None

#: The tables gaining the column set, and it is deliberately the SAME set with
#: the same names and widths as `cached_package_files`: an audit that has to
#: learn a different vocabulary per cache is one nobody runs.
_TABLES = ("cached_binaries", "cached_provider_packages")

_COLUMNS = (
    ("attestation", sa.String(32)),
    ("uploaded_by", sa.String(320)),
    ("upload_reference", sa.String(500)),
    ("replaced_digest", sa.String(200)),
    ("replaced_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    for table in _TABLES:
        for name, type_ in _COLUMNS:
            op.add_column(table, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        for name, _ in _COLUMNS:
            op.drop_column(table, name)
