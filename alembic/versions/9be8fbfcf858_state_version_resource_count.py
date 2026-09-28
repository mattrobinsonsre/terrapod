"""state_versions.resource_count — real counts for `pulumi stack ls` (#1568)

Expand-only: one nullable column, no backfill. NULL means "not counted", which
is every version written before this and every Terraform one. That is
deliberately distinct from a counted zero, so a stack whose count is unknown
reports nothing rather than claiming to be empty — which is what the hard-coded
`resourceCount: 0` did.

Revision ID: 9be8fbfcf858
Revises: 58c3a8aca3a5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9be8fbfcf858"
down_revision: str | None = "58c3a8aca3a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("state_versions", sa.Column("resource_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("state_versions", "resource_count")
