"""An `engine` on an autodiscovery rule, and a `stack` on a workspace (#1570).

Pure expand: one new column with a server default, so a lagging replica during
a rolling upgrade neither sees nor needs it.

The default is `terraform`, which is what every existing rule already produces
-- a rule had no engine, so everything it materialised was Terraform/OpenTofu.
That makes this expand-only in BEHAVIOUR as well as in schema: no existing rule
changes what it discovers or what it creates.

`server_default` rather than a Python-side default alone, because the old
replica still writing rows during the upgrade does not know the column exists
and would otherwise insert NULL into a NOT NULL column.

`workspaces.stack` is the other half. A Pulumi directory normally holds several
stacks -- `Pulumi.dev.yaml` beside `Pulumi.prod.yaml` -- so for Pulumi the unit
of work is (directory, stack), not the directory. Autodiscovery's lifecycle
resolves a directory to ONE workspace, which for Pulumi is ambiguous, so the
stack is stored explicitly rather than parsed back out of the `project::stack`
name: that lookup is a SQL `WHERE`, where string-splitting a name is neither
portable nor safe, and a rename would silently change what the destroy path
believes it is looking at.

NULL means "not stack-scoped" -- every Terraform workspace, and any Pulumi
workspace created before this column existed.

Revision ID: e3f76c455ce6
Revises: e2450c5ecc86
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3f76c455ce6"
down_revision: str | Sequence[str] | None = "e2450c5ecc86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "autodiscovery_rules",
        sa.Column(
            "engine",
            sa.String(length=32),
            nullable=False,
            server_default="terraform",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("stack", sa.String(length=255), nullable=True),
    )
    # A rule can now be Pulumi, so it can template Pulumi's own setting. This
    # is what clears the last of #1813: before the engine column, templating it
    # would have stored a value that could never apply to anything the rule
    # created, which is what the bulk path answers 422 for.
    op.add_column(
        "autodiscovery_rules",
        sa.Column(
            "pulumi_bind_plan",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("autodiscovery_rules", "pulumi_bind_plan")
    op.drop_column("workspaces", "stack")
    op.drop_column("autodiscovery_rules", "engine")
