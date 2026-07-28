"""multi-pool workspace routing (#1085 / #960 phase 0)

A workspace can route runs to more than one agent pool. The pool set is
`[agent_pool_id] + agent_pool_extra_ids` and is FLAT — every pool in it is
equally eligible to claim a run.

Expand-only. `workspaces.agent_pool_id` and `runs.pool_id` keep their existing
meaning (element 0 / the currently-associated pool), so an API replica still
running the previous release writes coherent rows throughout a rolling upgrade.

Revision ID: 9b7161d25c9e
Revises: af5d2a3c7bda
Create Date: 2026-07-28

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9b7161d25c9e"
down_revision: str | None = "af5d2a3c7bda"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "agent_pool_extra_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "pool_extra_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    # The claim predicate is `pool_id = :p OR pool_extra_ids @> '["<p>"]'`, and
    # the containment half needs a GIN index to stay a lookup rather than a
    # sequential scan of every queued run.
    op.create_index(
        "ix_runs_pool_extra_ids",
        "runs",
        ["pool_extra_ids"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_runs_pool_extra_ids", table_name="runs")
    op.drop_column("runs", "pool_extra_ids")
    op.drop_column("workspaces", "agent_pool_extra_ids")
