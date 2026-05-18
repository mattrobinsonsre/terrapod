"""autodiscovery_rules: var_files + run_task/notification templates (#318)

Lets a rule provision run tasks, notification configs and var files on
every workspace it materialises, so autodiscovered workspaces are fully
configured at creation (no second pass). Existing rows default to empty
lists.

Revision ID: 3547bfb4e898
Revises: 7dac5695bc0e
Create Date: 2026-05-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "3547bfb4e898"
down_revision: str | None = "7dac5695bc0e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY = sa.text("'[]'::jsonb")


def upgrade() -> None:
    for col in ("var_files", "run_task_templates", "notification_templates"):
        op.add_column(
            "autodiscovery_rules",
            sa.Column(col, JSONB, nullable=False, server_default=_EMPTY),
        )


def downgrade() -> None:
    for col in ("notification_templates", "run_task_templates", "var_files"):
        op.drop_column("autodiscovery_rules", col)
