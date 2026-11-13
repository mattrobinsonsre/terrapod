"""Scheduled configure runs (#2538)

Revision ID: 069d08e4effa
Revises: a7197ada2c0e
Create Date: 2026-11-13

Expand-only: four nullable-or-defaulted columns on `workspaces`, no backfill and
no contraction, so an older replica serving traffic beside a migrated database
is unaffected. A workspace that predates this has `scheduled_configure_enabled`
false, which is the behaviour it had before the column existed.

`downgrade` drops all four. That is lossy in the ordinary way -- a configured
schedule is gone and must be re-entered -- but it loses nothing an older
Terrapod could have read, so there is nothing to preserve.
"""

import sqlalchemy as sa
from alembic import op

revision = "069d08e4effa"
down_revision = "a7197ada2c0e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "scheduled_configure_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "scheduled_configure_cron",
            sa.String(length=200),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "scheduled_configure_timezone",
            sa.String(length=64),
            nullable=False,
            server_default="UTC",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "scheduled_configure_last_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # The same three settable columns on the autodiscovery rule, which templates
    # workspace settings onto everything it materialises. `last_at` has no
    # counterpart: it is a result, and a rule carries settings only.
    op.add_column(
        "autodiscovery_rules",
        sa.Column(
            "scheduled_configure_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "autodiscovery_rules",
        sa.Column(
            "scheduled_configure_cron",
            sa.String(length=200),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "autodiscovery_rules",
        sa.Column(
            "scheduled_configure_timezone",
            sa.String(length=64),
            nullable=False,
            server_default="UTC",
        ),
    )


def downgrade() -> None:
    op.drop_column("autodiscovery_rules", "scheduled_configure_timezone")
    op.drop_column("autodiscovery_rules", "scheduled_configure_cron")
    op.drop_column("autodiscovery_rules", "scheduled_configure_enabled")
    op.drop_column("workspaces", "scheduled_configure_last_at")
    op.drop_column("workspaces", "scheduled_configure_timezone")
    op.drop_column("workspaces", "scheduled_configure_cron")
    op.drop_column("workspaces", "scheduled_configure_enabled")
