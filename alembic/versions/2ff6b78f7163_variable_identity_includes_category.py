"""A variable is identified by (key, category), not by key alone

Revision ID: 2ff6b78f7163
Revises: 9be8fbfcf858
Create Date: 2026-09-29

Category was an attribute hanging off a variable rather than part of its name,
so a workspace could hold only one variable per key whatever the category. Three
things followed, and the third is why this is being fixed now (#1898):

- an operator could not add `pulumi_config:region` beside an existing
  `terraform:region`, which is exactly what moving a workspace between engines
  looks like -- the old one had to be deleted first, live, with no way to stage
  the change;
- a workspace variable silently *removed* a variable-set variable of the same key
  in another category, because `resolve_variables` layered them into one dict
  keyed on key alone. Not deprioritised: absent from the run;
- nothing in the delivery pipeline could ask "would this engine consume this?",
  because a variable carried no engine dimension to ask about -- so four layers
  each hardcoded their own category list instead (#1897).

Widening the key fixes the first two and makes the third expressible.

**This widens; it never narrows.** Every existing row already satisfies the new
constraint, because anything unique on (workspace_id, key) is unique on
(workspace_id, key, category) too. No backfill, no data change, no row is
touched. An old replica running against the new schema keeps working -- it
simply never creates a colliding row itself, which is why dropping a *unique*
constraint is safe under expand/contract in a way that dropping a column is not.

The downgrade is real and is the interesting half: it can only succeed while no
colliding pair exists. It checks first and raises with the offending rows named,
rather than letting Postgres fail with a constraint violation that says nothing
about which variables to reconcile. Deleting a "loser" to force it through would
be silent data loss, so it refuses instead.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "2ff6b78f7163"
down_revision: str | None = "9be8fbfcf858"
branch_labels: str | None = None
depends_on: str | None = None


#: (table, constraint, owning column) for the two variable tables. A variable
#: set's variables are keyed by their set, a workspace's by their workspace, and
#: both had the same defect.
_SCOPES = (
    ("variables", "uq_variables_workspace_key", "workspace_id"),
    ("variable_set_variables", "uq_variable_set_variables", "variable_set_id"),
)


def upgrade() -> None:
    for table, constraint, owner in _SCOPES:
        op.drop_constraint(constraint, table, type_="unique")
        op.create_unique_constraint(constraint, table, [owner, "key", "category"])


def downgrade() -> None:
    conn = op.get_bind()
    for table, constraint, owner in _SCOPES:
        # Name the rows that block the narrowing, so an operator can reconcile
        # them deliberately. Dropping one for them would be data loss.
        collisions = conn.execute(
            sa.text(
                f"SELECT {owner}, key, COUNT(*) AS n "  # noqa: S608 - identifiers are literals above
                f"FROM {table} GROUP BY {owner}, key HAVING COUNT(*) > 1"  # noqa: S608
            )
        ).fetchall()
        if collisions:
            detail = ", ".join(
                f"{owner}={row[0]} key={row[1]!r} ({row[2]} rows)" for row in collisions
            )
            raise RuntimeError(
                f"Cannot narrow {constraint}: {len(collisions)} key(s) in {table} exist in more "
                f"than one category, which the older constraint forbids. Reconcile them first "
                f"by removing the category you no longer want -- this migration will not choose "
                f"for you. Offending: {detail}"
            )
        op.drop_constraint(constraint, table, type_="unique")
        op.create_unique_constraint(constraint, table, [owner, "key"])
