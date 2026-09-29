"""The engine-named variable categories collapse into one: `native`

Revision ID: 611f0d7f501f
Revises: 2ff6b78f7163
Create Date: 2026-09-29

Each engine has exactly one channel for "the parameters the platform supplies to
this run" -- Terraform's input variables, Pulumi's stack config, Ansible's extra
vars. One role, three delivery mechanisms. Naming a category per engine named the
mechanism rather than the role, and made every new engine cost a category, a wire
list, a Secret key, a mount, a UI option, 32 locales and an SDK change (#1898).

So there is one category, and the runner dispatches delivery on the workspace's
engine. `pulumi_config` carried exactly the fields a `terraform` variable
already had -- key, value, sensitive, structured -- under three renamed keys,
which is the clearest evidence it was never a separate kind of thing.

The canonical name is `native`. `terraform` remains accepted and returned for
ever on the TFE-compatible surface, because `tfci` and `go-tfe` send it and
that contract is frozen -- exactly the arrangement `structured` already has with
`hcl` (#1435), where the column carries the honest name and the wire carries the
compatible one.

**One narrow case loses a row.** A workspace holding BOTH `terraform:region` and
`pulumi_config:region` collapses to two `native:region`, which the unique
constraint forbids. The oldest survives and the rest are removed, printed as they
go -- the same rule the identity migration uses, and for the same reason: the
older row is the one that predates whatever created the collision. In practice
this is nobody, since `pulumi_config` existed for about two hours, but a
migration is not permitted to be wrong about a case merely because it is rare.

The downgrade maps `native` back to `terraform`. Which rows were once
`pulumi_config` is not recoverable and is not recorded: a rollback is an
emergency, the old release cannot deliver Pulumi config from that category
anyway, and inventing a way back would be guessing.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "611f0d7f501f"
down_revision: str | None = "2ff6b78f7163"
branch_labels: str | None = None
depends_on: str | None = None

#: (table, owning column) for the two tables carrying variable categories.
_TABLES = (("variables", "workspace_id"), ("variable_set_variables", "variable_set_id"))

#: What collapses into `native`. `env` and the two git-auth categories are
#: different roles entirely and are untouched.
_COLLAPSING = ("terraform", "pulumi_config")


def upgrade() -> None:
    conn = op.get_bind()
    for table, owner in _TABLES:
        # Dedupe FIRST: after the collapse these would violate the unique
        # constraint, and a migration that fails half way through is worse than
        # one that says what it removed.
        losers = conn.execute(
            sa.text(
                f"SELECT id, {owner} AS owner, key, category FROM ("  # noqa: S608 - identifiers are literals above
                f"  SELECT id, {owner}, key, category, ROW_NUMBER() OVER ("  # noqa: S608
                f"    PARTITION BY {owner}, key ORDER BY id"  # noqa: S608
                f"  ) AS rn FROM {table} WHERE category IN :cats"  # noqa: S608
                f") ranked WHERE rn > 1"
            ).bindparams(sa.bindparam("cats", expanding=True)),
            {"cats": list(_COLLAPSING)},
        ).fetchall()

        if losers:
            for row in losers:
                print(
                    f"  collapse: removing {table} {row.category}:{row.key!r} "
                    f"({owner}={row.owner}) -- it would collide with the surviving "
                    f"native variable of the same key"
                )
            conn.execute(
                sa.text(f"DELETE FROM {table} WHERE id = ANY(:ids)"),  # noqa: S608
                {"ids": [row.id for row in losers]},
            )

        conn.execute(
            sa.text(
                f"UPDATE {table} SET category = 'native' WHERE category IN :cats"  # noqa: S608
            ).bindparams(sa.bindparam("cats", expanding=True)),
            {"cats": list(_COLLAPSING)},
        )


def downgrade() -> None:
    conn = op.get_bind()
    for table, _owner in _TABLES:
        conn.execute(
            sa.text(f"UPDATE {table} SET category = 'terraform' WHERE category = 'native'")  # noqa: S608
        )
