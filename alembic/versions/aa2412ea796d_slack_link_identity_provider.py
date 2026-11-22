"""Create the IdP column the Slack identity link has always declared.

Revision ID: aa2412ea796d
Revises: 24a459aa2107
Create Date: 2026-11-22

`SlackIdentityLink.identity_provider` was added to the model after
GHSA-3m8x-ff8g-7x8c -- role assignments are keyed (provider, email), so
resolving a Slack action on email alone gave it the union of every role that
address held under any provider -- and no migration ever created it. The model
therefore declared a column the schema did not have, so on any Alembic-built
database every query against the table failed. Model-vs-migration drift is
invisible to the test tiers, which build their tables from `Base.metadata`
(#2665).

Expand-only: nullable, so it applies to a database holding rows.

The backfill attributes only the links whose provider is derivable with
certainty, exactly as `96aa68186309` does for API tokens, and deliberately
leaves the rest NULL. A NULL resolves to no roles rather than to all of them;
inferring a provider is the guess the vulnerability was built on.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "aa2412ea796d"
down_revision: str | None = "24a459aa2107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "slack_identity_links", sa.Column("identity_provider", sa.String(63), nullable=True)
    )

    # A terrapod_email matching a users row that holds a password_hash is a local
    # account: SSO identities are not stored in that table at all, so the match
    # cannot be a coincidence of address.
    op.execute(
        """
        UPDATE slack_identity_links AS l
           SET identity_provider = 'local'
          FROM users AS u
         WHERE l.terrapod_email = u.email
           AND u.password_hash IS NOT NULL
           AND l.identity_provider IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("slack_identity_links", "identity_provider")
