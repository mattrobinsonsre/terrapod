"""drop `inventory_include_platform` and `inventory_ignore_paths` (#2256)

A contraction, deliberately, and safe only because both columns are main-only:
absent from `release/v1.11` and `release/v1.10`, so no operator is running a
deployment that has them.

Revision ID: 992da95f6179
Revises: 56e69e603265
Create Date: 2026-10-27

`inventory_include_platform` becomes unconditional. With no rows declared the
renderer emits `all:` alone and that resolves cleanly, so the flag had nothing
to switch off that being empty did not already express -- measured against
ansible-core, including the git-source-plus-empty-platform case.

`inventory_ignore_paths` goes because Ansible already owns the control:
`INVENTORY_IGNORE_PATTERNS` in the operator's own `ansible.cfg` is honoured,
since we set no `ANSIBLE_CONFIG` and force no ignore settings. That is the
better home -- versioned beside the inventory it describes, and in force for a
practitioner running `ansible-inventory` against the same tree, where a
Terrapod-side ignore applied only when resolving through Terrapod and so made
local and central resolution disagree. Note the syntaxes differ: Ansible's
patterns are REGEXES, where this column took globs.

`downgrade` restores both with their original server defaults, so this is a
true inverse of `7f1b648caf08`, which is left untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "992da95f6179"
down_revision: str | None = "56e69e603265"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Both tables carried the pair, because an autodiscovery rule templates a
#: workspace and the settings were templated with everything else.
_TABLES = ("workspaces", "autodiscovery_rules")


def upgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "inventory_ignore_paths")
        op.drop_column(table, "inventory_include_platform")


def downgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "inventory_include_platform",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "inventory_ignore_paths",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )
