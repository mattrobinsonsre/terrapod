"""Add run_id and created_by to state_versions.

Revision ID: c5734d5c739d
Revises: 49bb8c2d2fb3
Create Date: 2026-04-01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "c5734d5c739d"
down_revision = "49bb8c2d2fb3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "state_versions",
        sa.Column("run_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "state_versions",
        sa.Column("created_by", sa.String(255), nullable=True),
    )
    op.create_foreign_key(
        "fk_state_versions_run_id",
        "state_versions",
        "runs",
        ["run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_state_versions_run_id", "state_versions", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_state_versions_run_id", table_name="state_versions")
    op.drop_constraint("fk_state_versions_run_id", "state_versions", type_="foreignkey")
    op.drop_column("state_versions", "created_by")
    op.drop_column("state_versions", "run_id")
