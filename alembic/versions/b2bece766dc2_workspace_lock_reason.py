"""Why a workspace is locked, and who locked it (#1705).

Pure expand: two nullable columns with no default, so existing rows need no
backfill and an older replica mid-rolling-upgrade reads and writes workspaces
without knowing they exist. A lock taken by an older replica simply records no
reason or holder, which the API reports as null.

`lock_reason` is what the operator gave when locking (a maintenance note, or the
operation the terraform/tofu CLI reported); `locked_by` is the identity that
took the lock. Both are meaningful only while `locked` is true.

Revision ID: b2bece766dc2
Revises: 0bf0f9292aa8
"""

import sqlalchemy as sa
from alembic import op

revision = "b2bece766dc2"
down_revision = "0bf0f9292aa8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("lock_reason", sa.Text(), nullable=True))
    op.add_column("workspaces", sa.Column("locked_by", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("workspaces", "locked_by")
    op.drop_column("workspaces", "lock_reason")
