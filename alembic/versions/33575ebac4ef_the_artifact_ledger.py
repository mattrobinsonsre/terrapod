"""The artifact ledger: every artifact a runner was served, by digest

#2589 step 2's foundation. A durable, auditable record of what executed and who
vouched for each of it -- not a scan report, which is a snapshot of an opinion
rather than a record of a fact.

Keyed on the digest because that is the artifact's identity; coordinates are a
name somebody chose for it, and the same bytes can legitimately arrive under
several. It is the PRIMARY KEY rather than a unique column beside a surrogate
id, so that the table's identity and its replication identity are the same
thing -- see the model's own note for why a surrogate would never converge.

`sbom` is nullable and `matches` defaults to empty: the ledger is useful before
an inventory exists, and a row written today must not wait on Syft being wired.
Everything else is NOT NULL, because a ledger entry is only ever written by code
that already knows -- unlike the cache rows, where NULL is the load-bearing
value meaning "admitted before verification existed".

No `schema_contractions` row: this creates a table and removes nothing.

Revision ID: 33575ebac4ef
Revises: 667fd9da2621
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "33575ebac4ef"
down_revision: str | None = "667fd9da2621"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_ledger",
        # The digest IS the key -- no surrogate id. Replication keys this class
        # on `pk_attrs`, and the matrix gate requires those to equal the real
        # primary key, because a shorter declared key collapses sibling rows at
        # a failover. With a surrogate id, two nodes that each recorded the same
        # artifact would hold two rows with different ids and never converge.
        # 80 chars holds `sha512:` plus 128 hex with room spare.
        sa.Column("digest", sa.String(length=80), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "coordinates",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("attestation", sa.String(length=32), nullable=False),
        sa.Column(
            "attestor",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "admission",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        # Nullable: an entry predates its inventory, and will until 2b lands.
        sa.Column("sbom", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "matches",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "verdict_provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Indexed on what the admin surface filters by, and on `admitted_at` because
    # "what was admitted since" is the question an audit asks.
    op.create_index("ix_artifact_ledger_kind", "artifact_ledger", ["kind"])
    op.create_index("ix_artifact_ledger_attestation", "artifact_ledger", ["attestation"])
    op.create_index("ix_artifact_ledger_admitted_at", "artifact_ledger", ["admitted_at"])


def downgrade() -> None:
    # Dropping the ledger loses the record of what executed, which nothing else
    # holds -- the cache rows say what is cached NOW, not what was served and
    # since reaped. Stated rather than implied: this downgrade is lossy and the
    # loss is the whole point of the table.
    op.drop_index("ix_artifact_ledger_admitted_at", table_name="artifact_ledger")
    op.drop_index("ix_artifact_ledger_attestation", table_name="artifact_ledger")
    op.drop_index("ix_artifact_ledger_kind", table_name="artifact_ledger")
    op.drop_table("artifact_ledger")
