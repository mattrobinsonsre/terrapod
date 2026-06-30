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
    # Reverse to VARCHAR(255). NOTE: only safe when encryption has been disabled
    # and the column decrypted first — an encrypted envelope exceeds 255 chars.
    op.alter_column(
        "vcs_connections",
        "webhook_secret",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
