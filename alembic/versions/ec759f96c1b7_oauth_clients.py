"""Add oauth_clients

Registered OAuth clients for the `client_credentials` grant (#1108), used by
the HA peer link: each node registers a client representing its peer and hands
over those credentials, so the two authenticate with a standard grant rather
than a bespoke handshake.

The secret is SHA-256 hashed at rest — the same contract as an API token.

Purely additive: a new table, nothing altered, no contraction.

Revision ID: ec759f96c1b7
Revises: 3d76eac29cf2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ec759f96c1b7"
down_revision: str | None = "3d76eac29cf2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("client_secret_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="peer"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", name="uq_oauth_clients_client_id"),
    )
    op.create_index("ix_oauth_clients_client_id", "oauth_clients", ["client_id"])


def downgrade() -> None:
    op.drop_index("ix_oauth_clients_client_id", table_name="oauth_clients")
    op.drop_table("oauth_clients")
