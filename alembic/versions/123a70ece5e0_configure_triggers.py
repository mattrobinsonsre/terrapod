"""How a configure starts, and what one run was asked to configure

Revision ID: 123a70ece5e0
Revises: e5865425cd57
Create Date: 2026-10-22

#1974. The run machinery could already execute a configure and nothing could
start one: `runs.is_configure` existed with no writer, because triggering was
deliberately left to this change.

**On `runs`.** `configure_after_apply` is what tells the claim loop whether the
run needs a plan -- a standalone configure is claimed straight from `queued`
into `configuring`, a composite is claimed as a plan, then an apply, then a
configure -- so without it the plan tuple's `is_configure IS false` clause means
a composite can never plan at all. `playbook` and `ansible_limit` are what THIS
run was asked to do, on the run for the reason `target_addrs` is: the answer has
to survive the workspace's default moving. Empty means "the workspace's
default", which is how a platform-decided configure (a push carries no run
options) runs the declared defaults.

**On `workspaces` and `autodiscovery_rules`.** The two opt-ins, both default
off: `auto_configure_enabled` for the git-driven trigger, and
`configure_after_apply` for an apply that is followed by a configure on the same
run. The rule templates them because it already templates every other Ansible
setting, and a rule that could declare a configure operation without saying how
it starts would leave an operator editing each materialised workspace by hand.

There is deliberately no scheduled configure and so no schedule column (user,
2026-10-22).

Expand-only, and the defaults are the compatibility story: every existing row
reads false and empty, which is exactly today's behaviour -- no configure is
triggered by anything until an operator opts in. A replica running the previous
release ignores all five columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "123a70ece5e0"
down_revision = "e5865425cd57"
branch_labels = None
depends_on = None

#: The workspace owns the opt-ins and the rule templates them onto the
#: workspaces it materialises -- the pair every other per-workspace Ansible
#: setting lands on.
_DECLARING_TABLES = ("workspaces", "autodiscovery_rules")

#: What a run was asked to configure. Names only, because a `sa.Column` is
#: consumed by the `op.add_column` it is passed to and cannot be reused -- the
#: columns are built where they are added.
_RUN_COLUMNS = ("configure_after_apply", "playbook", "ansible_limit")


def upgrade() -> None:
    for table in _DECLARING_TABLES:
        op.add_column(
            table,
            sa.Column(
                "auto_configure_enabled", sa.Boolean(), nullable=False, server_default="false"
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "configure_after_apply", sa.Boolean(), nullable=False, server_default="false"
            ),
        )
    op.add_column(
        "runs",
        sa.Column("configure_after_apply", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("runs", sa.Column("playbook", sa.String(500), nullable=False, server_default=""))
    op.add_column(
        "runs", sa.Column("ansible_limit", sa.String(500), nullable=False, server_default="")
    )


def downgrade() -> None:
    for name in reversed(_RUN_COLUMNS):
        op.drop_column("runs", name)
    for table in _DECLARING_TABLES:
        op.drop_column(table, "configure_after_apply")
        op.drop_column(table, "auto_configure_enabled")
