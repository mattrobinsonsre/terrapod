"""workspace slack_channel (#556)

Per-workspace Slack channel for interactive run-approval / auto-apply / errored /
drift notifications. Opt-in: empty default → this workspace posts nothing (there
is no config-level fan-out). Additive; no behaviour change for existing
workspaces.

Revision ID: 5cf23a543399
Revises: 526699f9e991
Create Date: 2026-07-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5cf23a543399"
down_revision: str | None = "526699f9e991"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("slack_channel", sa.String(length=128), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "slack_channel")
