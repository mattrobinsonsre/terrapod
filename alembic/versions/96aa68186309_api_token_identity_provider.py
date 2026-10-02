"""Record the IdP on an API token so role resolution can join on it.

Revision ID: 96aa68186309
Revises: e0498e07de5c
Create Date: 2026-10-02

Role assignments are keyed (provider, email), but API-token role resolution
queried on email alone, so a token minted after a login at one provider
inherited every role assigned to that address under *any* provider
(GHSA-3m8x-ff8g-7x8c). The join needs the token to remember its provider.

Expand-only: the column is nullable and nothing reads it until the resolver
change in the same release. The backfill attributes only the tokens whose
provider is derivable with certainty -- an owner with a local `users` row that
holds a password is a local account, because SSO users are not stored in that
table at all -- and deliberately leaves the rest NULL. A NULL resolves to no
roles rather than to all of them: inferring a provider is exactly the guess the
vulnerability was built on.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "96aa68186309"
down_revision: str | None = "e0498e07de5c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_tokens", sa.Column("identity_provider", sa.String(63), nullable=True))

    # Attribute the tokens we can prove. A bound_to matching a users row that
    # holds a password_hash is a local account: SSO users have no row there, so
    # the match cannot be a coincidence of address.
    op.execute(
        """
        UPDATE api_tokens AS t
           SET identity_provider = 'local'
          FROM users AS u
         WHERE t.bound_to = u.email
           AND u.password_hash IS NOT NULL
           AND t.identity_provider IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("api_tokens", "identity_provider")
