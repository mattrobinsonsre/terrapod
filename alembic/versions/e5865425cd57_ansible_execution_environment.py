"""Add ansible_execution_environment to workspaces and autodiscovery rules

Revision ID: e5865425cd57
Revises: fe8c74c89dd5
Create Date: 2026-10-19

A workspace may name an operator-supplied execution environment by **label**
(#2154), which overrides its `ansible_version`. A label rather than a raw image
reference, so a workspace cannot name an arbitrary image and so an estate can be
re-pointed in one place.

It is a separate column rather than an overloaded `ansible_version`, and that is
measured rather than aesthetic: `is_pypi_version_allowed` screens only
pre-releases, so `'rhel8-hardened'` passes it -- and so would a label named
`'2.21.5'`, which would shadow a real ansible-core line invisibly.

Nothing is added to `runs`. A run already snapshots the **resolved** `ee_image`,
which is what identifies the environment after the fact; the label is a
selection input and would be a second, staler way to say the same thing.

Expand-only: a nullable-equivalent column with an empty server default, so a
replica running the previous release reads it as "no label" and behaves exactly
as before.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5865425cd57"
down_revision = "fe8c74c89dd5"
branch_labels = None
depends_on = None

#: The workspace owns the setting and the rule templates it onto the workspaces
#: it materialises -- the same pair every other per-workspace runner setting
#: lands on.
_TABLES = ("workspaces", "autodiscovery_rules")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "ansible_execution_environment",
                sa.String(100),
                nullable=False,
                server_default="",
            ),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "ansible_execution_environment")
