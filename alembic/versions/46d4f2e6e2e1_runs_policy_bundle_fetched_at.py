"""runs.policy_bundle_fetched_at — rolling-upgrade safety for OPA (#343)

Stamps a per-run timestamp when the runner GETs ``/policy-bundle``. The
post-plan gate uses this as a "the runner is policy-capable" signal:
during a rolling upgrade where a pre-#343 runner image is still cached
on a node, no bundle fetch would happen and a mandatory policy could
silently fail open. With this column the gate can distinguish
"no applicable sets" from "runner never tried" and fail closed for the
latter.

Revision ID: 46d4f2e6e2e1
Revises: 5a173d4b4e20
Create Date: 2026-05-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "46d4f2e6e2e1"
down_revision: str | None = "5a173d4b4e20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "policy_bundle_fetched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "policy_bundle_fetched_at")
