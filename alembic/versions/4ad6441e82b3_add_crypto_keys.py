"""add crypto_keys for app-layer encryption at rest (#553)

Revision ID: 4ad6441e82b3
Revises: e9503007edfc
Create Date: 2026-06-30

Stores KEK-wrapped data-encryption keys (one row per DEK version) for optional
application-layer encryption at rest. Off by default — the table stays empty
until an operator enables encryption. The encrypted columns themselves (e.g.
``certificate_authority.ca_key_pem``) remain ``TEXT`` and need no migration.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "4ad6441e82b3"
down_revision = "e9503007edfc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crypto_keys",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("wrapped_dek", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("canary", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("version", name="uq_crypto_keys_version"),
    )


def downgrade() -> None:
    # GHSA-mjhj-g43q-w6m9 (D1). Dropping this table destroys the ONLY copy of
    # the wrapped DEK, and every `tpenc:` value in the database then becomes
    # permanently unreadable — holding the operator master key does not help,
    # because the KEK unwraps a DEK that no longer exists.
    #
    # The docstring above says the table "stays empty until an operator enables
    # encryption". That was true and unenforced, so the safe case and the
    # catastrophic one ran the same code path silently. Now only the safe one
    # does.
    #
    # The documented escape hatch is `encryption_migrate decrypt` before
    # downgrading. Note it was itself incomplete until the ENCRYPTED_COLUMNS
    # fix in this same release: two columns were never visited, so an operator
    # who followed the procedure exactly would still have left
    # `gpg_keys.private_key` and `run_tasks.hmac_key` encrypted, and then
    # destroyed the key.
    rows = op.get_bind().execute(sa.text("SELECT count(*) FROM crypto_keys")).scalar()
    if rows:
        raise RuntimeError(
            f"refusing to drop crypto_keys: it holds {rows} key row(s), and they are "
            "the only copy of the DEK that decrypts every `tpenc:` value in this "
            "database.\n"
            "  1. Run `terrapod encryption_migrate decrypt` and verify it reported "
            "every encrypted column (it converts VALUES; it does not touch this "
            "table).\n"
            "  2. Confirm nothing is still encrypted: no column should contain a "
            "value starting `tpenc:`.\n"
            "  3. Only then DELETE FROM crypto_keys — at that point the rows decrypt "
            "nothing and removing them is the deliberate final step, not a "
            "workaround.\n"
            "Deleting them BEFORE step 2 destroys the data permanently; the master "
            "key does not help, because it unwraps a DEK that would no longer exist."
        )
    op.drop_table("crypto_keys")
