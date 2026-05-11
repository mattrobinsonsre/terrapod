"""add plan summary counts to runs (#301)

Adds five nullable integer columns to `runs` that hold the
resource-change counts parsed from the JSON plan output:
`resource_additions`, `resource_changes`, `resource_destructions`,
`resource_replacements`, `resource_imports`.

Null = not yet computed (older runs, or parse failed). Zero = computed,
no resources of that kind. This lets the UI distinguish "we don't know"
from "no changes".

Revision ID: 00b4c01e70e7
Revises: a0b6c95a281d
Create Date: 2026-05-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "00b4c01e70e7"
down_revision: str | None = "a0b6c95a281d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("resource_additions", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("resource_changes", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("resource_destructions", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("resource_replacements", sa.Integer(), nullable=True))
    op.add_column("runs", sa.Column("resource_imports", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "resource_imports")
    op.drop_column("runs", "resource_replacements")
    op.drop_column("runs", "resource_destructions")
    op.drop_column("runs", "resource_changes")
    op.drop_column("runs", "resource_additions")
