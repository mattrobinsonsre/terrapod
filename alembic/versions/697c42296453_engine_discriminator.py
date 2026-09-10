"""Add the engine discriminator to workspaces

#1407 phase 1 (#1487). Expand-only: the column is added with a server default of
``terraform``, so every existing row is correct without a backfill and an older
replica mid-rollout never reads a value it cannot interpret. Nothing to contract
later — there is no old column being replaced.

``engine`` is deliberately *not* ``execution_backend``, which already sits on
workspaces and picks the binary (tofu vs terraform) *within* the Terraform
family. This names the family itself.

**Workspaces only (#1536).** This migration originally added the column to
``configuration_versions`` and ``runs`` too, as "the three tables a run's
identity flows through". Neither copy earned its place. Engine is identity —
replace-forcing, never edited — so there is nothing mid-flight to snapshot, and
everything that needs it can reach the workspace: a configuration version only
ever through it, a run by a join (the reconciler's one query per cycle) or from
a workspace its caller already holds. A stored copy is also how #1523 happened:
a run's copy defaulted to ``terraform`` and sent Pulumi runs down the Terraform
path, reporting success.

The other two columns were removed by amending this migration in place rather
than by a contraction. That is sound only because the migration had shipped in
no release, so no deployment ran it and no replica could have read the columns.
Do not repeat that move on a released migration.

Revision ID: 697c42296453
Revises: edd2bcb183de
"""

import sqlalchemy as sa

from alembic import op

revision = "697c42296453"
down_revision = "edd2bcb183de"
branch_labels = None
depends_on = None

#: The one table that stores the engine. Everything else joins to it.
_TABLES = ("workspaces",)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "engine",
                sa.String(length=20),
                nullable=False,
                server_default="terraform",
            ),
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, "engine")
