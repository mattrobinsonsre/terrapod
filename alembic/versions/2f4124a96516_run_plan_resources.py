"""runs.plan_resources: the bounded list of resources a plan changes

Revision ID: 2f4124a96516
Revises: 26175f851307
Create Date: 2026-11-10

The PR status comment says THAT three resources change and not WHICH three, so a
reviewer typing `terrapod apply` in the pull request had to leave it to find out.
The comment cannot read the plan JSON at render time, and should not: the
payload size is decided by the user's infrastructure. So the list is recorded
once, when the plan JSON lands, already bounded (at most 25 resources, 8
attributes per update, 120 characters per value, with a count of what was left
out) and already redacted.

One JSONB column holding `{"entries": [...], "omitted": N}`. The five
`resource_*` count columns are UNTOUCHED, so every existing reader behaves as it
did.

Expand-only: one nullable column, no backfill. A run that predates this has
NULL, which the comment reads as "no list" and renders as today. NULL and an
empty list are different answers on purpose: NULL is "not recorded" (an older
run, a Pulumi preview, an unreadable plan), and `{"entries": []}` is the true
statement that the plan changes nothing.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "2f4124a96516"
down_revision = "26175f851307"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("plan_resources", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "plan_resources")
