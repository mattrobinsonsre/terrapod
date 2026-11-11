"""schema_contractions: let an old pod detect a contraction it predates

Revision ID: f9bb1c338a1c
Revises: 4393ae805ce2
Create Date: 2026-11-11

An API pod stays READY when the schema is ahead of its code (#2500), because
that is the normal state of a rolling upgrade and an additive migration leaves
the old code working. A **contracting** migration does not: a column the old
code still reads may be gone.

This table is how the old pod finds out. A contracting migration inserts its own
revision id, and a pod refuses readiness when the table holds a revision its own
revision graph does not contain. No ordering between revisions is needed, which
matters because the contracting revision is by definition unknown to the code
being gated: if the pod has never heard of it, the contraction is newer than the
pod; if the pod's graph contains it, the pod is at or after it.

It is deliberately not an ORM model. Like `alembic_version` it is schema
plumbing read with raw SQL rather than application data, so it carries no
replication registration and no model-parity obligations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9bb1c338a1c"
down_revision: str | None = "4393ae805ce2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schema_contractions",
        sa.Column("revision", sa.String(length=64), primary_key=True),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("schema_contractions")
