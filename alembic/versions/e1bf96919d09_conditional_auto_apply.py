"""Conditional auto-apply modes (#1274)

Adds `auto_apply_mode` alongside the existing `auto_apply` boolean on
workspaces, autodiscovery-rule templates and runs, plus a declined-reason on
runs.

Expand-only: `auto_apply` is NOT dropped. It stays the boolean projection
that un-upgraded clients read and write, and an old API replica mid-rolling-
upgrade keeps writing it — so both columns must exist together. Nothing to
contract here, now or later.

The backfill maps the boolean onto the enum so an existing auto-applying
workspace comes out as `always` and keeps behaving identically.

Revision ID: e1bf96919d09
Revises: 0c328eb42b84
"""

import sqlalchemy as sa
from alembic import op

revision = "e1bf96919d09"
down_revision = "0c328eb42b84"
branch_labels = None
depends_on = None

_MODE = sa.String(length=20)
_TABLES = ("workspaces", "autodiscovery_rules", "runs")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "auto_apply_mode",
                _MODE,
                nullable=False,
                server_default="never",
            ),
        )
        # Existing rows: true -> always, false -> never. Identical behaviour.
        op.execute(
            f"UPDATE {table} SET auto_apply_mode = 'always' WHERE auto_apply = true"  # noqa: S608
        )

    op.add_column(
        "runs",
        sa.Column("auto_apply_declined_reason", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    # `auto_apply` was never dropped and has been kept in step with the mode
    # on every write path, so dropping these columns loses only the
    # conditional refinement — a workspace on `create`/`create_update`
    # reverts to plain auto-apply. Flatten those to false first: reverting a
    # deployment must not silently promote a conditional workspace to
    # unconditional auto-apply.
    op.execute(
        "UPDATE workspaces SET auto_apply = false "
        "WHERE auto_apply_mode IN ('create', 'create_update')"
    )
    op.execute(
        "UPDATE autodiscovery_rules SET auto_apply = false "
        "WHERE auto_apply_mode IN ('create', 'create_update')"
    )
    op.execute(
        "UPDATE runs SET auto_apply = false WHERE auto_apply_mode IN ('create', 'create_update')"
    )
    op.drop_column("runs", "auto_apply_declined_reason")
    for table in _TABLES:
        op.drop_column(table, "auto_apply_mode")
