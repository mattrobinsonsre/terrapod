"""Rename the variable `hcl` flag to `structured` (#1435).

A straight rename rather than expand/contract, chosen deliberately.

The cost is real and worth stating: migrations run as a Helm `pre-upgrade` hook,
so they complete before any new pod starts. For the length of the pod rollout the
new schema is live while old replicas are still serving, and those replicas query
`hcl` — so variable reads and writes fail until the rollout finishes. That blip
was accepted in preference to carrying a duplicated column and a dual-write path
through a release.

Nothing above the database changes: `/api/v2` still accepts and returns `hcl`,
for ever, because `tfci` and `go-tfe` send and read it. That compatibility is
the API layer's job and needs no column of its own — conflating the two is what
made the first draft of this migration keep a column nothing would have read.
"""

from alembic import op

revision = "4408d25e7130"
down_revision = "0ad9785105ed"
branch_labels = None
depends_on = None

_TABLES = ("variables", "variable_set_variables")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, "hcl", new_column_name="structured")


def downgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, "structured", new_column_name="hcl")
