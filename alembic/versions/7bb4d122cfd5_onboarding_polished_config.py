"""onboarding session AI-polished config (#824 Phase A)

Adds ``polished_config`` and ``polished_import_blocks`` to
``onboarding_sessions``: the optional AI-polished view of the discovery output
(resources renamed from their tags, grouped, commented). Stored SEPARATELY from
the deterministic ``generated_config`` / ``import_blocks`` so the raw,
guaranteed-import-clean version is never lost and the UI can toggle raw↔polished.
Both are nullable and null on existing rows, so this is additive and inert — a
session with no polish behaves exactly as before (``ai_assisted`` already gates
whether a polish exists).

Revision ID: 7bb4d122cfd5
Revises: 0153ea0a8bfc
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7bb4d122cfd5"
down_revision: str | None = "0153ea0a8bfc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "onboarding_sessions",
        sa.Column("polished_config", sa.Text(), nullable=True),
    )
    op.add_column(
        "onboarding_sessions",
        sa.Column("polished_import_blocks", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("onboarding_sessions", "polished_import_blocks")
    op.drop_column("onboarding_sessions", "polished_config")
