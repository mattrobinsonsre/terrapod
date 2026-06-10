"""Add workspaces.drift_ignore_rules.

Per-workspace allowlist of resource-address-plus-attribute-path glob
patterns that are excluded from drift classification (#482). Stored as
a JSONB list of strings so the validation happens in Python and the
database-side schema stays simple.

Default `'[]'::jsonb` so existing workspaces inherit the prior
behaviour (every plan change counts as drift). Setting this to a
non-empty list takes effect on the next drift run via the classifier
in `drift_ignore_classifier.py` — the column is read on the
`handle_drift_run_completed` path; nothing materialises until then.

Revision ID: 56599efa894a
Revises: 204f61b07907
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "56599efa894a"
down_revision = "204f61b07907"


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "drift_ignore_rules",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "drift_ignore_rules")
