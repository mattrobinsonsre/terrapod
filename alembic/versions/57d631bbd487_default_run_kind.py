"""workspaces/rules.default_run_kind: which run button is primary

Revision ID: 57d631bbd487
Revises: 9c864f64ed11
Create Date: 2026-11-10

`availableRunKinds` offers the most complete kind a workspace declares and the
first becomes the primary button. That is right almost always, and wrong for a
workspace whose Terraform half only maintains the Ansible inventory: with
`auto_apply` on and `configure_after_apply` off, the everyday action is
Configure while the primary button is the composite.

An empty string is "today's ordering", so every existing workspace and every
existing rule behaves exactly as before and no backfill is needed.

Expand-only: one NOT NULL column per table with a server default of `""`.
"""

import sqlalchemy as sa
from alembic import op

revision = "57d631bbd487"
down_revision = "9c864f64ed11"
branch_labels = None
depends_on = None

#: The two tables, by their REAL names. The autodiscovery rule's class is
#: `WorkspaceAutodiscoveryRule` and its table is `autodiscovery_rules` -- the
#: `workspace_` prefix exists only in the class name, and a sibling class
#: (`ModuleAutodiscoveryRule`) does own `module_autodiscovery_rules`, so the
#: wrong guess is a plausible one. This migration first carried it, and no pytest
#: tier could see that: the integration tier builds its tables from
#: `Base.metadata` rather than by running the chain.
_TABLES = ("workspaces", "autodiscovery_rules")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("default_run_kind", sa.String(length=20), nullable=False, server_default=""),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "default_run_kind")
