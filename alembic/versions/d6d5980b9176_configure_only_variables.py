"""configure-only variables: the flag, and both uniqueness constraints widened (#2190)

A variable flagged configure-only is delivered to a configure and withheld from
a plan AND from an apply, so a machine credential reaching the fleet -- an SSH
private key to the estate -- is never in the environment of either.

**This is a THIRD slot, not a rename of the second, because a configure already
receives every apply-only row.** `PHASES_SEEING_APPLY_ONLY` is
`{"apply", "configure"}` (#2200), decided on the grounds that a configure runs
the repository's playbooks against real hosts and is therefore a write with
apply-equivalent blast radius -- so withholding a credential from it would
withhold it from the one phase that has anything to do with it. `apply_only`
consequently cannot express "not in an apply either", and that is exactly what a
machine credential wants: nothing in a plan or an apply has any business holding
one.

So the three states a key can be held in are: ordinary (every phase), apply-only
(apply and configure), configure-only (configure alone).

**Two booleans rather than a three-valued field**, following #2189 and the
workspace operation flags (#1966) for the recorded reason: a parsed value kept
in step across the API, the SDK, the provider, MCP and the UI gets mistyped into
a third meaning eventually, and a boolean cannot be. The cost is that both-true
is representable; it is refused at the write boundary rather than being given a
resolution nobody would predict.

**The flag has to join both uniqueness tuples.** `(workspace_id, key, category,
apply_only)` and `(variable_set_id, key, category, apply_only)` each forbid an
ordinary row and a configure-only row of the same key outright today, and
holding both is the entire delivery pattern. Four columns to five.

The three dimensions are mutually orthogonal: `category` separates an input
variable from an environment variable of the same name, `apply_only` separates
the plan's copy from the apply's, and `configure_only` separates the fleet's
credential from both.

**Widening is behaviour-preserving.** With every row at `false` the new
constraints are equivalent to the old ones, and the only way to obtain a
duplicate is for somebody to set the flag. No deployment gains a row it could
not previously hold.

**NOT NULL with a server_default, and that is not cosmetic.** Postgres treats
NULLs as DISTINCT in a unique constraint, so a nullable column left NULL on
existing rows would silently stop both constraints catching duplicates for
exactly those rows -- the protection would switch off for the whole
pre-migration table while the constraint still appeared to be there. The
backfill has to be in the database, not in a Python-side default.

**A LATER MIGRATION THAT DROPS AND RECREATES THESE CONSTRAINTS MUST LIST
`configure_only` TOO.** That is not hypothetical: `2ff6b78f7163` (#1898)
recreated both of them without `apply_only` after `7a27d4747b9e` had added it,
so a database migrated from scratch carried a three-column constraint while the
models declared four and the whole of #2189 was dead at the database level. No
pytest tier can see it -- every other one builds its tables from
`Base.metadata` rather than by running the chain -- which is why
`tests/integration/test_migrated_schema_matches_models.py` exists and is where a
repeat would be caught.

The `drop_constraint` calls are `upgrade()`-side contractions and are ledgered
deliberately. Replacing a narrow unique constraint with a wider one is
permissive, so a rolling upgrade is safe in the direction that matters: an old
replica that could not violate the old constraint cannot violate the new one,
and it writes no configure-only rows because it does not know the flag.

`main` only. Configure operations do not exist on the 1.10 or 1.11 lines, so
there is nothing to carry and no release head to record.

Revision ID: d6d5980b9176
Revises: 992da95f6179
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6d5980b9176"
down_revision: str | None = "992da95f6179"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The owning column of each table, so the two constraint rebuilds below are one
#: statement of the rule rather than two copies of it.
_TABLES = (
    ("variables", "uq_variables_workspace_key", "workspace_id"),
    ("variable_set_variables", "uq_variable_set_variables", "variable_set_id"),
)


def upgrade() -> None:
    for table, _constraint, _owner in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "configure_only",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    for table, constraint, owner in _TABLES:
        op.drop_constraint(constraint, table, type_="unique")
        op.create_unique_constraint(
            constraint,
            table,
            [owner, "key", "category", "apply_only", "configure_only"],
        )


def downgrade() -> None:
    # Narrowing back is only possible while no ordinary/configure-only pair
    # exists, which is the honest shape: there is no way to hold both rows AND
    # the constraint that forbids them. A downgrade with a pair present fails on
    # the constraint rather than silently dropping one of the two rows, so an
    # operator is told what is in the way instead of losing a credential.
    for table, constraint, owner in reversed(_TABLES):
        op.drop_constraint(constraint, table, type_="unique")
        op.create_unique_constraint(constraint, table, [owner, "key", "category", "apply_only"])
    for table, _constraint, _owner in reversed(_TABLES):
        op.drop_column(table, "configure_only")
