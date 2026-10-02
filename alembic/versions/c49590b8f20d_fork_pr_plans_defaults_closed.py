"""Fork-PR plans default closed

The 1.8 line shipped `allow_fork_pr_plans` defaulting TRUE, deliberately: a patch
release must not stop a fork pull request that plans today. 1.9.0 is the minor that
takes the secure default, so the column default follows the code.

**Existing rows are not rewritten.** A workspace created before this upgrade keeps
whatever it has, which for a 1.8 deployment means `true`. Flipping stored values
would disable, without asking, a plan an operator may depend on — and on a
workspace whose repository takes no outside pull requests there is nothing to fix.
So this closes the default and leaves the audit to the operator;
`docs/security-hardening.md` has the query and the bulk update.

Both tables move together. They were split once already — the column defaulted one
way and the create path the other — so two workspaces on the same repository
behaved differently depending on how they came to exist, and an autodiscovery rule
that kept the old default would re-open the setting the next time it created a
workspace.

Revision ID: c49590b8f20d
Revises: 0ad52f633ab0
Create Date: 2026-10-01

"""

import sqlalchemy as sa
from alembic import op

revision: str = "c49590b8f20d"
down_revision: str | None = "0ad52f633ab0"
branch_labels: str | None = None
depends_on: str | None = None

#: Both carry the setting: the workspace it applies to, and the autodiscovery rule
#: that stamps it onto every workspace it creates.
_TABLES = ("workspaces", "autodiscovery_rules")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "allow_fork_pr_plans",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.false(),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.alter_column(
            table,
            "allow_fork_pr_plans",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.true(),
        )
