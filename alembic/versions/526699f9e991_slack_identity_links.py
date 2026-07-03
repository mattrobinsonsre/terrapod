"""slack identity links (#556)

Durable binding of a Slack user (team_id + user_id) to a Terrapod identity
(email), established once via explicit login and reused for every subsequent
Slack-initiated action. New table; no change to existing behaviour.

Revision ID: 526699f9e991
Revises: 1429df8c538a
Create Date: 2026-07-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "526699f9e991"
down_revision: str | None = "1429df8c538a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "slack_identity_links",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("slack_team_id", sa.String(length=32), nullable=False),
        sa.Column("slack_user_id", sa.String(length=32), nullable=False),
        sa.Column("terrapod_email", sa.String(length=320), nullable=False),
        sa.Column("linked_via", sa.String(length=32), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("slack_team_id", "slack_user_id", name="uq_slack_identity"),
    )


def downgrade() -> None:
    op.drop_table("slack_identity_links")
