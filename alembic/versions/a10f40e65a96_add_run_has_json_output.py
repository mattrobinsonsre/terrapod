"""add runs.has_json_output

Tracks whether `tofu show -json` output was successfully uploaded for a
run, so plan responses can advertise the json-output URL only when the
artifact actually exists. See terrapod #280.

Revision ID: a10f40e65a96
Revises: 8bb13bfef824
Create Date: 2026-05-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a10f40e65a96"
down_revision: str | None = "8bb13bfef824"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "has_json_output",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "has_json_output")
