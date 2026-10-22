"""configure_all_hosts: the override that widens a derived configure

Revision ID: bafb1c60d9fc
Revises: 4b2c80e3ecc1
Create Date: 2026-10-22

Expand-only. Defaults false on both tables, so an existing workspace keeps
today's behaviour: with no baseline recorded yet, a derived configure goes wide
anyway (#2229's condition 3), and the flag only matters once baselines exist.
"""

import sqlalchemy as sa
from alembic import op

revision = "bafb1c60d9fc"
down_revision = "4b2c80e3ecc1"
branch_labels = None
depends_on = None

_TABLES = ("workspaces", "autodiscovery_rules")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "configure_all_hosts",
                sa.Boolean(),
                nullable=False,
                server_default="false",
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "configure_all_hosts")
