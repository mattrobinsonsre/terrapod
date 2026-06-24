"""Add per-connection webhook_secret to vcs_connections.

A single global vcs.github.webhook_secret validated webhooks for every GitHub
installation, so any installation that learned the one secret could forge a
valid signature for any other. Add an optional per-connection secret; when set
it validates that connection's inbound webhooks, falling back to the global
secret when null (so existing single-secret deployments are unaffected).

Revision ID: e9503007edfc
Revises: 166efacb7b5d
"""

import sqlalchemy as sa

from alembic import op

revision = "e9503007edfc"
down_revision = "166efacb7b5d"


def upgrade() -> None:
    op.add_column("vcs_connections", sa.Column("webhook_secret", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("vcs_connections", "webhook_secret")
