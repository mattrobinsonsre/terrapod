"""Add state_diverged column to workspaces.

Tracks when an apply Job succeeds but the state upload to object storage
fails, indicating potential infrastructure/state divergence.

Revision ID: 8529adc11ddb
Revises: f3e984cc98cf
Create Date: 2026-03-18
"""

import sqlalchemy as sa
from alembic import op

revision = "8529adc11ddb"
down_revision = "f3e984cc98cf"


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "state_diverged",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "state_diverged")
