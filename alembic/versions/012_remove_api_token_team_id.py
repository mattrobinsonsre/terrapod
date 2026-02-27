"""Remove unused team_id column from api_tokens.

Terrapod uses label-based RBAC instead of TFE-style teams.
The team_id column was dead code inherited from the TFE schema.

Revision ID: 012
Revises: 011
Create Date: 2026-02-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("api_tokens", "team_id")


def downgrade() -> None:
    op.add_column(
        "api_tokens",
        sa.Column("team_id", sa.String(63), nullable=True),
    )
