"""Vault as a variable value source (#1439).

Pure expand: one column on each variable table, defaulted to "static" so every
existing row keeps exactly its current behaviour — the value in `value` is the
value. Only a row explicitly set to "vault" is treated as a reference.
"""

import sqlalchemy as sa
from alembic import op

revision = "b7c41e05d9a2"
down_revision = "d9dff757b0a4"
branch_labels = None
depends_on = None

_TABLES = ("variables", "variable_set_variables")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "value_source",
                sa.String(length=20),
                nullable=False,
                server_default="static",
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "value_source")
