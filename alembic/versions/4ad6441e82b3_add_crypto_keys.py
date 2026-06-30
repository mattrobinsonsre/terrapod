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
    op.drop_table("crypto_keys")
