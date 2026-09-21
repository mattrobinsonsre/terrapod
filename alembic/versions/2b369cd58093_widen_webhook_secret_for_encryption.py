"""widen vcs_connections.webhook_secret VARCHAR(255)->TEXT for encryption (#553)

Revision ID: 2b369cd58093
Revises: 4ad6441e82b3
Create Date: 2026-06-30

Phase-1 breadth flips several secret columns to EncryptedText. The only one that
was length-bounded is ``vcs_connections.webhook_secret`` (VARCHAR(255)) — an
encryption envelope is longer than the plaintext, so a near-limit secret would
**overflow and corrupt** the value. Widen it to TEXT (a no-rewrite, instant
change on Postgres) before any value can be encrypted. The other newly-encrypted
columns (variables.value, variable_set_variables.value, vcs_connections.token,
notification_configurations.token) are already TEXT and need no change.
"""

import sqlalchemy as sa
from alembic import op

revision = "2b369cd58093"
down_revision = "4ad6441e82b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "vcs_connections",
        "webhook_secret",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    # Reverse to VARCHAR(255). Only safe when encryption has been disabled and
    # the column decrypted first — the comment said so and nothing enforced it
    # (GHSA-mjhj-g43q-w6m9, D4), so the failure was a raw Postgres 22001
    # halfway through a downgrade chain.
    #
    # It is not universal, which is why a length check beats a blanket refusal:
    # the envelope is roughly 28 + ceil(4/3 x (len + 16)), so a short secret
    # still fits and the break-even plaintext is around 155 characters. Under
    # the old VARCHAR(255) an operator could legitimately store up to 255, so a
    # real slice of deployments sit in the failing band.
    #
    # Worth knowing why this one matters beyond its own message: this migration
    # sits directly above the crypto_keys one, so a downgrade heading past the
    # encryption work hits THIS failure first. An operator who resolves it by
    # shortening or deleting the row and re-running then loses the DEK to D1.
    # The louder failure hides the destructive one.
    bind = op.get_bind()
    too_long = bind.execute(
        sa.text(
            "SELECT count(*) FROM vcs_connections "
            "WHERE webhook_secret IS NOT NULL AND length(webhook_secret) > 255"
        )
    ).scalar()
    if too_long:
        raise RuntimeError(
            f"refusing to narrow vcs_connections.webhook_secret: {too_long} row(s) "
            "exceed 255 characters and would be truncated. Run `terrapod "
            "encryption_migrate decrypt` first — and do NOT work around this by "
            "deleting the rows, because the next downgrade in the chain drops the "
            "encryption key table."
        )
    op.alter_column(
        "vcs_connections",
        "webhook_secret",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
