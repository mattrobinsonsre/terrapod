"""Run triggers: cross-workspace dependency chains.

When a source workspace completes an apply, all downstream workspaces
with an inbound trigger automatically get a new run queued.

Revision ID: 8c8a6a056ce2
Revises: 65ec20e8d634
Create Date: 2026-03-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8c8a6a056ce2"
down_revision: Union[str, None] = "65ec20e8d634"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "run_triggers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "workspace_id", "source_workspace_id", name="uq_run_triggers"
        ),
    )
    op.create_index(
        "ix_run_triggers_source_workspace_id",
        "run_triggers",
        ["source_workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_run_triggers_source_workspace_id", table_name="run_triggers")
    op.drop_table("run_triggers")
