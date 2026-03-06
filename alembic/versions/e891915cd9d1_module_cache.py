"""Module caching table (no-op — table removed in later migration).

Revision ID: e891915cd9d1
Revises: 0890044564cb
Create Date: 2026-02-27
"""

from typing import Sequence, Union

revision: str = "e891915cd9d1"
down_revision: Union[str, None] = "0890044564cb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
