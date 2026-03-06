"""Remove application-layer encryption.

Rename *_encrypted columns to plaintext names and drop encrypted_value
columns from variables tables (sensitive vars now stored in `value`).

Revision ID: 2db753c16b9d
Revises: ca96f0af3fb2
Create Date: 2026-03-06
"""

import sqlalchemy as sa
from alembic import op

revision = "2db753c16b9d"
down_revision = "ca96f0af3fb2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename token_encrypted → token on vcs_connections
    op.alter_column("vcs_connections", "token_encrypted", new_column_name="token")

    # Rename token_encrypted → token on notification_configurations
    op.alter_column("notification_configurations", "token_encrypted", new_column_name="token")

    # Rename hmac_key_encrypted → hmac_key on run_tasks
    op.alter_column("run_tasks", "hmac_key_encrypted", new_column_name="hmac_key")

    # Rename private_key_encrypted → private_key on gpg_keys
    op.alter_column("gpg_keys", "private_key_encrypted", new_column_name="private_key")

    # Drop encrypted_value from variables (sensitive vars now use value column)
    op.drop_column("variables", "encrypted_value")

    # Drop encrypted_value from variable_set_variables
    op.drop_column("variable_set_variables", "encrypted_value")


def downgrade() -> None:
    # Re-add encrypted_value columns
    op.add_column(
        "variable_set_variables",
        sa.Column("encrypted_value", sa.Text(), nullable=True),
    )
    op.add_column(
        "variables",
        sa.Column("encrypted_value", sa.Text(), nullable=True),
    )

    # Rename back
    op.alter_column("gpg_keys", "private_key", new_column_name="private_key_encrypted")
    op.alter_column("run_tasks", "hmac_key", new_column_name="hmac_key_encrypted")
    op.alter_column("notification_configurations", "token", new_column_name="token_encrypted")
    op.alter_column("vcs_connections", "token", new_column_name="token_encrypted")
