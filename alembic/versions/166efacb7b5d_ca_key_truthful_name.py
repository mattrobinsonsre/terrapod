"""Rename certificate_authority.ca_key_encrypted to ca_key_pem.

The column never held an application-encrypted value — it is a NoEncryption
PKCS8 PEM, protected at rest by database encryption (same as sensitive
variables and VCS tokens). Rename it so the schema doesn't claim a property
it doesn't have. Pure rename, no data change.

Revision ID: 166efacb7b5d
Revises: a675f90f82e7
"""

from alembic import op

revision = "166efacb7b5d"
down_revision = "a675f90f82e7"


def upgrade() -> None:
    op.alter_column("certificate_authority", "ca_key_encrypted", new_column_name="ca_key_pem")


def downgrade() -> None:
    op.alter_column("certificate_authority", "ca_key_pem", new_column_name="ca_key_encrypted")
