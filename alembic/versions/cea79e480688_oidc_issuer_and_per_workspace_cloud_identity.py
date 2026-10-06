"""OIDC issuer signing keys and per-workspace cloud identity

Revision ID: cea79e480688
Revises: 15a8b636ea54
Create Date: 2026-10-05

Two additive things for #1901.

`oidc_signing_keys` holds the RSA keypairs Terrapod signs run identity tokens
with. Several rows rather than one, because a published trust root cannot be
swapped atomically: the clouds fetch the JWKS on their own schedule, so a
rotation publishes a key, waits, and only then signs with it — which means more
than one key is live at a time.

`oidc_audiences` on workspaces is the opt-in and the one genuinely per-workspace
value: the audiences a run identity token is minted for. Empty (the default, and
what every existing row gets) means the workspace mints nothing and its runs
authenticate to the cloud exactly as before. The same column is templated on
autodiscovery rules and snapshotted onto runs, matching `resource_cpu`.

Expand-only. Nothing is dropped, nothing is retyped, and every column is
NOT NULL with a server default so an older replica writing a row during a
rolling upgrade still produces a valid one.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "cea79e480688"
down_revision = "15a8b636ea54"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oidc_signing_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # RFC 7638 JWK thumbprint: 43 base64url characters for SHA-256. Sized at
        # 64 so a future digest does not need a migration.
        sa.Column("kid", sa.String(length=64), nullable=False),
        sa.Column("private_key_pem", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activates_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("kid", name="uq_oidc_signing_keys_kid"),
    )
    op.create_index("ix_oidc_signing_keys_kid", "oidc_signing_keys", ["kid"])

    # A JSON **object**, not an array: provider name (optionally
    # `provider.alias`) to that target's audiences, because one token is minted
    # per target. Edited in place rather than altered by a follow-up migration —
    # this revision has never shipped (v1.10 is unreleased and untagged), so
    # there is no deployment holding the array shape and no contraction to
    # ledger. Do NOT edit it again once a tag exists.
    for table in ("workspaces", "autodiscovery_rules", "runs"):
        op.add_column(
            table,
            sa.Column(
                "oidc_audiences",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default="{}",
            ),
        )


def downgrade() -> None:
    for table in ("runs", "autodiscovery_rules", "workspaces"):
        op.drop_column(table, "oidc_audiences")

    op.drop_index("ix_oidc_signing_keys_kid", table_name="oidc_signing_keys")
    op.drop_table("oidc_signing_keys")
