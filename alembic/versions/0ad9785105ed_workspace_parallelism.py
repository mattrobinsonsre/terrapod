"""How many operations an engine performs at once, per workspace (#1431).

Pure expand: three nullable-free columns with a server default, so an old replica
mid-rolling-upgrade reads and writes them happily without knowing they exist.

The default is 10 because that is terraform's own. Every existing workspace was
already running at 10 without being able to say so, and this column lets it say
so without changing what it does — which is the property that makes adding it
safe to ship in a minor.
"""

import sqlalchemy as sa
from alembic import op

revision = "0ad9785105ed"
down_revision = "792641f78049"
branch_labels = None
depends_on = None

_TABLES = ("workspaces", "runs", "autodiscovery_rules")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "parallelism",
                sa.Integer(),
                nullable=False,
                server_default="10",
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "parallelism")
