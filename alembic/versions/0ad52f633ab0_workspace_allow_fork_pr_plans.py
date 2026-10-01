"""allow_fork_pr_plans on workspaces and on the autodiscovery rule template.

A pull request opened from a fork executes its author's code during a
speculative plan, with everything the run receives — env-category secrets,
sensitive variables, Vault-resolved values, minted git credentials and the
Job's cloud workload identity. A fork author has no write access to the base
repository and cannot merge, so that plan is the only path by which their code
ever runs against those credentials.

The rule carries it too, so an operator who opts in does not silently lose
the opt-in on the next directory autodiscovery finds.

Expand-only: two nullable-free booleans with server defaults, so an older
replica serving traffic during a rolling upgrade is unaffected — it simply
never reads the columns.

The default is `true` on this release line: a patch must not stop a fork PR
that plans today, so the column ships permissive and the operator turns it
off. 2.0 defaults it false. The release notes say so.

Revision ID: 0ad52f633ab0
Revises: bb495836611d
"""

import sqlalchemy as sa
from alembic import op

revision = "0ad52f633ab0"
down_revision = "bb495836611d"
branch_labels = None
depends_on = None


# On this release line the columns default TRUE, where the 2.x line defaults them
# false. The gate and all its plumbing are identical; only the default differs, so
# a patch cannot stop a fork pull request that plans today. An operator closes the
# hole by setting the workspace (or autodiscovery rule) to false, and 2.0 defaults
# it closed. See GHSA-gp5w-76rw-c452.
#
# Parented on bb495836611d, this line's recorded head, so the 1.8 chain stays a
# prefix of main's. `alembic_release_heads.json` moves to this revision with it.


def upgrade() -> None:
    for table in ("workspaces", "autodiscovery_rules"):
        op.add_column(
            table,
            sa.Column(
                "allow_fork_pr_plans",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )


def downgrade() -> None:
    for table in ("workspaces", "autodiscovery_rules"):
        op.drop_column(table, "allow_fork_pr_plans")
