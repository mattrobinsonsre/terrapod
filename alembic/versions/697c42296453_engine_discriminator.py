"""Add the engine discriminator to workspaces and runs

#1407 phase 1 (#1487). Expand-only: the column is added with a server default of
``terraform``, so every existing row is correct without a backfill and an older
replica mid-rollout never reads a value it cannot interpret. Nothing to contract
later — there is no old column being replaced.

``engine`` is deliberately *not* ``execution_backend``, which already sits on
both of these tables and picks the binary (tofu vs terraform) *within* the
Terraform family. This names the family itself.

**Not on ``configuration_versions`` (#1536).** This migration originally added it
there too, for symmetry, and nothing ever read it: a configuration version is
reachable only through its workspace, which carries the engine, and engine is
identity — replace-forcing, never edited — so there is no mid-flight change to
snapshot against. It was removed by amending this migration in place rather than
by a contraction, which is sound only because the migration had never shipped in
any release, so no deployment ever ran it and no replica could have read the
column. Do not repeat that move on a released migration.

Revision ID: 697c42296453
Revises: edd2bcb183de
"""

import sqlalchemy as sa

from alembic import op

revision = "697c42296453"
down_revision = "edd2bcb183de"
branch_labels = None
depends_on = None

#: Where the engine is read. The workspace owns it; the run carries a copy because
#: the reconciler holds a Run and no Workspace, every few seconds, over every
#: in-flight run — the one place a join would cost something.
_TABLES = ("workspaces", "runs")


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
