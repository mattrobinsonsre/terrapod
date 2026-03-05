"""Notification configurations: workspace-scoped run lifecycle notifications.

Supports generic webhooks (HMAC-SHA512 signed), Slack (Block Kit), and
email (SMTP) destinations. Fires asynchronously via the distributed scheduler.

Revision ID: 305686df7bbd
Revises: 49006be724e1
Create Date: 2026-03-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "305686df7bbd"
down_revision: Union[str, None] = "49006be724e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notification_configurations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("destination_type", sa.String(20), nullable=False),
        sa.Column("url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("token_encrypted", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "triggers",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "email_addresses",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "delivery_responses",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_notification_configurations_workspace_id",
        "notification_configurations",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_configurations_workspace_id",
        table_name="notification_configurations",
    )
    op.drop_table("notification_configurations")
