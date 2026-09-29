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

The downgrade **deletes** colliding rows rather than refusing, and that is a
deliberate call: a downgrade you cannot rely on is not a downgrade. A rollback
happens in an incident, and one that can refuse mid-way -- because of data an
operator added perfectly legitimately after upgrading -- leaves them stuck on a
release they are trying to escape. The point of the reversibility invariant
(#550) is that a bad release can always be rolled back.

The loss is bounded and the rule is not arbitrary: **the oldest row per
(owner, key) survives and the rest are deleted.** A collision can only exist
because a second variable was added *after* the upgrade -- the older constraint
forbade it -- so the oldest is precisely the row that existed before, and keeping
it restores the pre-upgrade state exactly. The rows removed are the ones the old
release could not represent at all.

Ordering is by `id`, which is a uuid7 and therefore time-ordered: byte order is
creation order, verified rather than assumed. `variables` also carries
`created_at` but `variable_set_variables` does not, so one rule covers both.

What is deleted is printed, because a migration that removes rows in an incident
should say which.
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
        # Keep the oldest per (owner, key); the rest cannot exist under the
        # narrower constraint. uuid7 ids sort by creation time.
        losers = conn.execute(
            sa.text(
                f"SELECT id, {owner} AS owner, key, category FROM ("  # noqa: S608 - identifiers are literals above
                f"  SELECT id, {owner}, key, category, ROW_NUMBER() OVER ("  # noqa: S608
                f"    PARTITION BY {owner}, key ORDER BY id"  # noqa: S608
                f"  ) AS rn FROM {table}"  # noqa: S608
                f") ranked WHERE rn > 1"
            )
        ).fetchall()

        if losers:
            for row in losers:
                print(
                    f"  downgrade: removing {table} {row.category}:{row.key!r} "
                    f"({owner}={row.owner}) -- the older constraint cannot hold it"
                )
            conn.execute(
                sa.text(f"DELETE FROM {table} WHERE id = ANY(:ids)"),  # noqa: S608
                {"ids": [row.id for row in losers]},
            )

        op.drop_constraint(constraint, table, type_="unique")
        op.create_unique_constraint(constraint, table, [owner, "key"])
