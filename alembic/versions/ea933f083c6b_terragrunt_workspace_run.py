"""Terragrunt single-unit support (#534): terragrunt_enabled + terragrunt_version.

Adds the per-workspace Terragrunt toggle + version (partial, resolved via the
binary cache like terraform_version; workspace default "1.0" = latest 1.0.x).
The same pair is snapshotted onto `runs` at run creation so an in-flight run's
execution tool is frozen against later workspace edits (matching how
execution_backend / terraform_version are snapshotted).

Revision ID: ea933f083c6b
Revises: 2ad40c9b26b6
"""

import sqlalchemy as sa
from alembic import op

revision = "ea933f083c6b"
down_revision = "2ad40c9b26b6"


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "terragrunt_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "terragrunt_version",
            sa.String(20),
            nullable=False,
            server_default="1.0",
        ),
    )
    # Run snapshot: existing rows get the inert defaults (disabled, empty
    # version) — they were created before Terragrunt support existed.
    op.add_column(
        "runs",
        sa.Column(
            "terragrunt_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "terragrunt_version",
            sa.String(20),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "terragrunt_version")
    op.drop_column("runs", "terragrunt_enabled")
    op.drop_column("workspaces", "terragrunt_version")
    op.drop_column("workspaces", "terragrunt_enabled")
