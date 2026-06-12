"""Service-token model: kind, bound_to, created_by, rotated_at, pinned_roles (#495).

Phase 1 of scoped service tokens + expiry warnings. Adds the structured
token-kind distinction and splits the single `user_email` column into a
binding (`bound_to`, NULL <=> detached) and an audit field (`created_by`).

Ordered to be safe on a populated table:
  1. add `created_by` nullable, backfill `COALESCE(user_email, '')` (handles
     any legacy NULL-owner rows), then SET NOT NULL;
  2. rename `user_email` -> `bound_to` (values preserved, so every existing
     token stays *bound*, never silently detached) and re-create its index
     under the new name to avoid permanent model<->DB drift;
  3. add `kind` (existing rows default to 'interactive'), `rotated_at`,
     `pinned_roles`.
`token_type` is RETAINED (legacy, superseded by `kind`) for response
back-compat; dropped in a later release.

Revision ID: 3fbcb2885b93
Revises: 56599efa894a
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "3fbcb2885b93"
down_revision = "56599efa894a"


def upgrade() -> None:
    # 1. created_by — minter/audit record, distinct from the binding.
    op.add_column("api_tokens", sa.Column("created_by", sa.String(255), nullable=True))
    op.execute("UPDATE api_tokens SET created_by = COALESCE(user_email, '')")
    op.alter_column("api_tokens", "created_by", nullable=False, server_default="")

    # 2. user_email -> bound_to (NULL <=> detached). Drop the old-named index
    #    while the column still exists, rename, then create the new index.
    op.drop_index("ix_api_tokens_user_email", table_name="api_tokens")
    op.alter_column("api_tokens", "user_email", new_column_name="bound_to")
    op.create_index("ix_api_tokens_bound_to", "api_tokens", ["bound_to"])

    # 3. New columns.
    op.add_column(
        "api_tokens",
        sa.Column("kind", sa.String(20), nullable=False, server_default="interactive"),
    )
    op.add_column(
        "api_tokens",
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("api_tokens", sa.Column("pinned_roles", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("api_tokens", "pinned_roles")
    op.drop_column("api_tokens", "rotated_at")
    op.drop_column("api_tokens", "kind")

    op.drop_index("ix_api_tokens_bound_to", table_name="api_tokens")
    op.alter_column("api_tokens", "bound_to", new_column_name="user_email")
    op.create_index("ix_api_tokens_user_email", "api_tokens", ["user_email"])

    op.drop_column("api_tokens", "created_by")
