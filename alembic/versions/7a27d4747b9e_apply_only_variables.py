"""apply-only variables: the flag, and both uniqueness constraints widened (#2189)

A variable flagged apply-only is delivered to an apply and withheld from a
plan, so a credential only the apply needs is never in the environment of a
plan that executes a pull request's code.

The shape is DUPLICATION rather than a phase attribute: one boolean, and
"a different value per phase" is two rows -- `TOKEN` read-only ordinary,
`TOKEN` write-capable apply-only. That requires the flag to join both
uniqueness tuples, because `(workspace_id, key, category)` and
`(variable_set_id, key, category)` each forbid the pair outright today.

`category` is already in both tuples here (#1898), and it is orthogonal to this
flag: category separates an input variable from an environment variable of the
same name, while `apply_only` separates the plan's copy from the apply's. So the
constraints carry BOTH and go from three columns to four -- on the 1.x release
lines, which have no category in the tuple, the same migration goes from two to
three.

**Widening is behaviour-preserving.** With every row at `false` the new
constraints are equivalent to the old ones: existing pairs stay mutually
exclusive on `key`, and the only way to obtain a duplicate is for somebody
to set the flag. No deployment gains a row it could not previously hold.

**NOT NULL with a server_default, and that is not cosmetic.** Postgres
treats NULLs as DISTINCT in a unique constraint, so a nullable column left
NULL on existing rows would silently stop both constraints catching
duplicates for exactly those rows -- the protection would switch off for the
whole pre-migration table while the constraint still appeared to be there.
The backfill has to be in the database, not in a Python-side default.

The `drop_constraint` calls are `upgrade()`-side contractions and are
ledgered deliberately. Replacing a narrow unique constraint with a wider one
is permissive, so a rolling upgrade is safe in the direction that matters:
an old replica that could not violate the old constraint cannot violate the
new one, and it writes no apply-only rows because it does not know the flag.

Revision ID: 7a27d4747b9e
Revises: 3214ad466305
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a27d4747b9e"
down_revision: str | None = "3214ad466305"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("variables", "variable_set_variables"):
        op.add_column(
            table,
            sa.Column(
                "apply_only",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    # `(workspace_id, key)` -> `(workspace_id, key, apply_only)`.
    #
    # `category` is deliberately absent, even though `main` ends up with it in
    # this constraint. At THIS point in the chain it is not in the constraint
    # yet: `2ff6b78f7163` (#1898) adds it later and exists only on `main`. So
    # this revision does exactly what it does on the 1.11 release line, and the
    # two lines converge at `2ff6b78f7163`, which widens to all four. One
    # revision id, one effect, whichever line runs it.
    op.drop_constraint("uq_variables_workspace_key", "variables", type_="unique")
    op.create_unique_constraint(
        "uq_variables_workspace_key",
        "variables",
        ["workspace_id", "key", "apply_only"],
    )

    # `(variable_set_id, key)` -> `(variable_set_id, key, apply_only)`.
    op.drop_constraint("uq_variable_set_variables", "variable_set_variables", type_="unique")
    op.create_unique_constraint(
        "uq_variable_set_variables",
        "variable_set_variables",
        ["variable_set_id", "key", "apply_only"],
    )


def downgrade() -> None:
    # Narrowing back is only possible while no duplicate pair exists, which is
    # the honest shape: there is no way to hold both rows AND the constraint
    # that forbids them. A downgrade with a pair present fails on the
    # constraint rather than silently dropping one of the two rows, so an
    # operator is told what is in the way instead of losing a credential.
    op.drop_constraint("uq_variable_set_variables", "variable_set_variables", type_="unique")
    op.create_unique_constraint(
        "uq_variable_set_variables", "variable_set_variables", ["variable_set_id", "key"]
    )
    op.drop_constraint("uq_variables_workspace_key", "variables", type_="unique")
    op.create_unique_constraint("uq_variables_workspace_key", "variables", ["workspace_id", "key"])
    for table in ("variable_set_variables", "variables"):
        op.drop_column(table, "apply_only")
