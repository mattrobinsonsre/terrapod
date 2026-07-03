"""autodiscovery execution_hook_templates (#672)

Adds a list of execution-hook ids that an autodiscovery rule associates with
every workspace it materialises, so discovered workspaces inherit their hooks
automatically (mirrors run_task_templates / notification_templates). Additive,
defaults to empty — no behaviour change for existing rules.

Revision ID: 1429df8c538a
Revises: 6cede1508fb7
Create Date: 2026-07-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "1429df8c538a"
down_revision: str | None = "6cede1508fb7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "autodiscovery_rules",
        sa.Column(
            "execution_hook_templates",
            JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("autodiscovery_rules", "execution_hook_templates")
