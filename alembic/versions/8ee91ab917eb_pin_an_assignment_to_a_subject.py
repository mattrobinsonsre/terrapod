"""Allow a role assignment to be pinned to an IdP subject.

Revision ID: 8ee91ab917eb
Revises: 96aa68186309
Create Date: 2026-10-02

Assignments are keyed (provider, email), and the previous revision made token role
resolution join on the provider. Email remains the weaker half: an IdP that lets a
user change their address, or an operator who recycles one, can move a grant to a
different human (GHSA-3m8x-ff8g-7x8c, the "long term key identity on provider and
subject" half).

A subject is the stable half of an identity and cannot be acquired by acquiring an
address. This adds it as an optional *restriction* on an existing assignment rather
than a new kind of row -- NULL keeps matching on (provider, email), which is what an
operator can actually type, since a `sub` is opaque. That is also why it is not part
of the primary key: it narrows a grant, it does not identify one.

Expand-only and inert until set: every existing row is NULL and behaves exactly as
before.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "8ee91ab917eb"
down_revision: str | None = "96aa68186309"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("role_assignments", sa.Column("subject", sa.String(255), nullable=True))
    op.add_column("platform_role_assignments", sa.Column("subject", sa.String(255), nullable=True))
    op.add_column("api_tokens", sa.Column("identity_subject", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("api_tokens", "identity_subject")
    op.drop_column("platform_role_assignments", "subject")
    op.drop_column("role_assignments", "subject")
