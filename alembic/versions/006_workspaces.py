"""Add workspaces and state_versions tables.

Revision ID: 006
Revises: 005
Create Date: 2026-02-24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_name", sa.String(63), nullable=False, server_default="default"),
        sa.Column("name", sa.String(90), nullable=False),
        sa.Column("execution_mode", sa.String(20), nullable=False, server_default="local"),
        sa.Column("auto_apply", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("terraform_version", sa.String(20), nullable=False, server_default=""),
        sa.Column("working_directory", sa.String(500), nullable=False, server_default=""),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("lock_id", sa.String(255), nullable=True),
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
        sa.UniqueConstraint("org_name", "name", name="uq_workspaces"),
    )

    op.create_table(
        "state_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("serial", sa.Integer(), nullable=False),
        sa.Column("lineage", sa.String(63), nullable=False, server_default=""),
        sa.Column("md5", sa.String(32), nullable=False, server_default=""),
        sa.Column("state_size", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("workspace_id", "serial", name="uq_state_versions"),
    )
    op.create_index("ix_state_versions_workspace_id", "state_versions", ["workspace_id"])


def downgrade() -> None:
    op.drop_table("state_versions")
    op.drop_table("workspaces")
