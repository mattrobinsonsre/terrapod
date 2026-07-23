"""cost_summaries.estimated_resources — AI estimates for what the engine can't price (#871)

The cost AI's PRIMARY output: the model's own monthly estimate for resources the
deterministic engine could not price (the unpriced bucket + providers the engine
doesn't cover + usage-driven dimensions it omits). Each entry is tagged
`source: "ai-estimate"`, shown as a separate overlay, and never summed into the
authoritative deterministic total.

Purely additive: a JSONB column with a `[]` server default, no backfill.

Revision ID: d321124f68ee
Revises: 05af36ae12f9
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d321124f68ee"
down_revision = "05af36ae12f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cost_summaries",
        sa.Column(
            "estimated_resources",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("cost_summaries", "estimated_resources")
