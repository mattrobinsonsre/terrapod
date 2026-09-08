"""Add the engine discriminator to the three run-lifecycle tables

#1407 phase 1 (#1487). Expand-only: the column is added with a server default of
``terraform``, so every existing row is correct without a backfill and an older
replica mid-rollout never reads a value it cannot interpret. Nothing to contract
later — there is no old column being replaced.

``engine`` is deliberately *not* ``execution_backend``, which already sits on two
of these tables and picks the binary (tofu vs terraform) *within* the Terraform
family. This names the family itself.

Revision ID: 697c42296453
Revises: edd2bcb183de
"""

import sqlalchemy as sa

from alembic import op

revision = "697c42296453"
down_revision = "edd2bcb183de"
branch_labels = None
depends_on = None

#: The three tables a run's identity flows through: the workspace it belongs to,
#: the configuration it runs, and the run itself.
_TABLES = ("workspaces", "configuration_versions", "runs")


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
