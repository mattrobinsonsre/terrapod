"""VCS connections get an owner, labels and an optional repository allowlist

GHSA-v8g7-pqrj-8mcm. v1.8.2 closed the escalation by requiring the caller to
already own a workspace on the connection. That works and has a deliberate
consequence: the FIRST workspace on a connection has to be created by a platform
admin, because nobody else can have a claim yet. These columns are the general
answer — a connection becomes a labelled resource like every other, so it can be
delegated to a team, and the allowlist closes the residual hole where an entitled
caller could still point it at any repository its credential can read.

Expand-only and additive. Every column is NOT NULL with a server default that
reproduces today's behaviour exactly: no owner, no labels, and an empty allowlist
meaning "any repository the credential can reach". An old API replica running
against this schema during a rolling upgrade neither reads nor writes them.

Revision ID: d0bdcaa15b45
Revises: c49590b8f20d
Create Date: 2026-10-01

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d0bdcaa15b45"
down_revision: str | None = "c49590b8f20d"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "vcs_connections",
        sa.Column("owner_email", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "vcs_connections",
        sa.Column(
            "labels",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "vcs_connections",
        sa.Column(
            "allowed_repositories",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("vcs_connections", "allowed_repositories")
    op.drop_column("vcs_connections", "labels")
    op.drop_column("vcs_connections", "owner_email")
