"""dynamic run-time inventory: two per-workspace switches

Revision ID: d70160a15ce0
Revises: d6d5980b9176
Create Date: 2026-10-29

Two boolean switches permitting ansible inventory plugins ON THE RUNNER (#2033),
never in the API. Separate columns rather than one, so an operator can take
declared plugin configs without also permitting arbitrary executables:

    dynamic_inventory_enabled   ansible's `auto`   -- a YAML file with `plugin:`
    inventory_scripts_enabled   ansible's `script` -- an executable script

Both default FALSE on every existing row, so this is inert until an operator
opts in: a workspace that does not set them keeps refusing plugin sources by
name, which is the behaviour before this migration.

EXPAND-ONLY, which is what makes it safe under a rolling upgrade: three columns
added with a server default, nothing dropped, renamed or retyped. An older
replica serving traffic alongside the new one simply never reads them.

The `runs` copy is a SNAPSHOT, the same shape as `ee_image` and the resource
requests: the runner composes its `ANSIBLE_INVENTORY_ENABLED` from what the run
was dispatched with, so the answer cannot change under a run whose workspace
setting moves mid-flight.
"""

import sqlalchemy as sa
from alembic import op

revision = "d70160a15ce0"
down_revision = "d6d5980b9176"
branch_labels = None
depends_on = None

#: Every table taking the pair, in the order a reader meets them: the setting,
#: the template that materialises it, and the per-run snapshot.
_TABLES = ("workspaces", "autodiscovery_rules", "runs")

_COLUMNS = ("dynamic_inventory_enabled", "inventory_scripts_enabled")


def upgrade() -> None:
    for table in _TABLES:
        for column in _COLUMNS:
            op.add_column(
                table,
                sa.Column(
                    column,
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("false"),
                ),
            )


def downgrade() -> None:
    # Reversible without loss of anything an operator would miss: both columns
    # are opt-in switches whose absence means the behaviour this migration
    # replaced. A workspace that had opted in goes back to refusing plugin
    # sources by name, which is a narrowing and therefore the safe direction.
    for table in reversed(_TABLES):
        for column in reversed(_COLUMNS):
            op.drop_column(table, column)
