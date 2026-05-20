"""workspace remote state consumers (#344)

Producer-controlled allowlist authorizing a consumer workspace's agent
runs to read a producer workspace's state via terraform_remote_state.
Empty for a producer => its state is not shared (secure by default);
no behaviour change for existing deployments.

Revision ID: 42266b856e8e
Revises: 3f0ad398be2c
Create Date: 2026-05-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "42266b856e8e"
down_revision: str | None = "3f0ad398be2c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_remote_state_consumers",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "producer_workspace_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "consumer_workspace_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(255), server_default="", nullable=False),
        sa.UniqueConstraint(
            "producer_workspace_id",
            "consumer_workspace_id",
            name="uq_workspace_remote_state_consumers",
        ),
    )
    op.create_index(
        "ix_wrsc_consumer_workspace_id",
        "workspace_remote_state_consumers",
        ["consumer_workspace_id"],
    )
    op.create_index(
        "ix_wrsc_producer_workspace_id",
        "workspace_remote_state_consumers",
        ["producer_workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_wrsc_producer_workspace_id", table_name="workspace_remote_state_consumers")
    op.drop_index("ix_wrsc_consumer_workspace_id", table_name="workspace_remote_state_consumers")
    op.drop_table("workspace_remote_state_consumers")
