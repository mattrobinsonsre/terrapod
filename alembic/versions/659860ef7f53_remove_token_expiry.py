"""Remove expired_at column from api_tokens.

Expiry is now computed at validation time from created_at + config max TTL,
not stored per-token in the database.

Revision ID: 659860ef7f53
Revises: 76c447c4c533
Create Date: 2026-02-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "659860ef7f53"
down_revision: Union[str, None] = "76c447c4c533"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("api_tokens", "expired_at")


def downgrade() -> None:
    op.add_column(
        "api_tokens",
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
    )
