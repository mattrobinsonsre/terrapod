"""Drop agent_pools.service_account_name column.

Per-pool ServiceAccount overrides are removed. Runner Jobs now use
the global ServiceAccount from Helm values (runners.serviceAccount.name).

Revision ID: 1f15ac58564c
Revises: b43b10a4da62
Create Date: 2026-03-12
"""

from alembic import op
import sqlalchemy as sa

revision = "1f15ac58564c"
down_revision = "b43b10a4da62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("agent_pools", "service_account_name")


def downgrade() -> None:
    op.add_column(
        "agent_pools",
        sa.Column("service_account_name", sa.String(63), nullable=False, server_default=""),
    )
