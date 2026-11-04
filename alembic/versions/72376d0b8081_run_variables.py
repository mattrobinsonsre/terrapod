"""Per-run `-var` values (#2377).

Revision ID: 72376d0b8081
Revises: a9a7825cce45
"""

import sqlalchemy as sa
from alembic import op

revision: str = "72376d0b8081"
down_revision: str | None = "a9a7825cce45"


def upgrade() -> None:
    # Expand-only: nullable, no backfill, no default. A run created before this
    # has no `-var` values by definition, and NULL says exactly that.
    #
    # TEXT rather than JSONB although it holds a JSON document: the column is
    # `EncryptedText`, which is a TEXT TypeDecorator and cannot wrap JSONB. See
    # the model, and `crypto/columns.py` where it is registered for re-keying.
    op.add_column("runs", sa.Column("run_variables", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "run_variables")
