"""Add execution_backend column to workspaces and runs.

Every workspace now has an explicit execution_backend (tofu/terraform)
and the value is snapshotted to runs at creation time. Existing rows
default to 'tofu'. Empty terraform_version values on workspaces are
updated to '1.9'.

Revision ID: 8d2c5b0c1edc
Revises: c4fc090289ee
Create Date: 2026-03-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d2c5b0c1edc"
down_revision: Union[str, None] = "c4fc090289ee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add execution_backend to workspaces
    op.add_column(
        "workspaces",
        sa.Column(
            "execution_backend",
            sa.String(20),
            nullable=False,
            server_default="tofu",
        ),
    )

    # Add execution_backend to runs
    op.add_column(
        "runs",
        sa.Column(
            "execution_backend",
            sa.String(20),
            nullable=False,
            server_default="tofu",
        ),
    )

    # Update empty terraform_version to '1.9' on workspaces
    op.execute(
        "UPDATE workspaces SET terraform_version = '1.9' WHERE terraform_version = ''"
    )


def downgrade() -> None:
    op.drop_column("runs", "execution_backend")
    op.drop_column("workspaces", "execution_backend")
