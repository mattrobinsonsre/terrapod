"""onboarding session provider version constraint (#824 P2)

Adds ``provider_version`` to ``onboarding_sessions``: an optional provider
version *constraint* (e.g. "< 6.0", "~> 5.0") the discovery pins in its generated
``providers.tf``. Empty = unconstrained (latest), so this is additive and inert
for existing rows and deployments — a session with no constraint behaves exactly
as before.

Revision ID: 0153ea0a8bfc
Revises: 58c33a46e7bc
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0153ea0a8bfc"
down_revision: str | None = "58c33a46e7bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "onboarding_sessions",
        sa.Column(
            "provider_version",
            sa.String(64),
            server_default="",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("onboarding_sessions", "provider_version")
