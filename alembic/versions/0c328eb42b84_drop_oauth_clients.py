"""Drop oauth_clients — the peer credential lives in config, not a row (#1171).

`oauth_clients` existed to hold the credential a node accepts from its peer,
hashed at rest and reconciled from config at startup. Both halves of that
credential are declared in the chart, so the row was a *copy* of what config
already stated — and a copy that could then disagree with it, which is what
made "rotate by editing the chart" a thing that had to be specially handled.

The CA is persisted because the node GENERATES the keypair and it must survive
a restart. Nothing is generated here: the operator supplies both halves, so
there is nothing to preserve. The grant now compares the presented credential
against config directly.

**A contraction, and deliberately not phased.** The expand/contract discipline
exists so an old replica still serving traffic during a rolling upgrade cannot
be broken by a schema change. It cannot bite here: the table was added after
v1.2.0 and removed before the next tag, so it appears in no release and no
deployed replica reads it. Recorded in the contraction ledger regardless, so
the acknowledgement is explicit rather than assumed.

`downgrade()` recreates the table so the chain is genuinely reversible, but it
cannot recreate the rows — nor does it need to, since nothing reads them.

Revision ID: 0c328eb42b84
Revises: 7359beafb7c0
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0c328eb42b84"
down_revision: str | None = "7359beafb7c0"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_index("ix_oauth_clients_client_id", table_name="oauth_clients")
    op.drop_table("oauth_clients")


def downgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("client_secret_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id"),
    )
    op.create_index("ix_oauth_clients_client_id", "oauth_clients", ["client_id"], unique=True)
