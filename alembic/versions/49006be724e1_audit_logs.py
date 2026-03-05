"""Audit logs: immutable API request log for compliance.

Captures actor, action, resource, status, and duration for every
non-health API request. Retention managed by a daily scheduler task.

Revision ID: 49006be724e1
Revises: 8c8a6a056ce2
Create Date: 2026-03-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "49006be724e1"
down_revision: Union[str, None] = "8c8a6a056ce2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("actor_email", sa.String(255), nullable=False, server_default=""),
        sa.Column("actor_ip", sa.String(45), nullable=False, server_default=""),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("resource_type", sa.String(63), nullable=False, server_default=""),
        sa.Column("resource_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(63), nullable=False, server_default=""),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_audit_logs_timestamp", "audit_logs", ["timestamp"])
    op.create_index("ix_audit_logs_actor_email", "audit_logs", ["actor_email"])
    op.create_index(
        "ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_resource", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_email", table_name="audit_logs")
    op.drop_index("ix_audit_logs_timestamp", table_name="audit_logs")
    op.drop_table("audit_logs")
