"""Autodiscovery rules template the scan and AI-summary settings (#1763).

An autodiscovery rule already templates most of what a workspace it materialises
will be -- execution mode and backend, engine version, resources, parallelism,
auto-apply, labels, owner, var-files, run tasks, notifications, execution hooks.
It did not template security scanning or the AI plan summary, so a rule covering
hundreds of directories could not opt them in at creation. Paired with the
absence of any apply-to-existing path (fixed separately in the bulk-update map),
there was no scalable way to set either at all.

Purely additive and defaulted to today's behaviour: every column takes the same
value the workspace-creation path already defaults to, so an existing rule
materialises exactly the workspace it materialised before this ran.

`security_scan_enforcement` needs no engine guard here, unlike on a workspace:
an autodiscovery rule has no `engine` column, so every workspace it materialises
is a Terraform/OpenTofu one, which is precisely the case that can be scanned.

Revision ID: a6751186f8b7
Revises: f9b3aac00aac
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a6751186f8b7"
down_revision: str | Sequence[str] | None = "f9b3aac00aac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "autodiscovery_rules",
        sa.Column(
            "security_scan_enforcement",
            sa.String(20),
            nullable=False,
            server_default="advisory",
        ),
    )
    op.add_column(
        "autodiscovery_rules",
        sa.Column("security_scan_engine", sa.String(20), nullable=False, server_default="checkov"),
    )
    op.add_column(
        "autodiscovery_rules",
        sa.Column(
            "security_scan_severity_threshold",
            sa.String(20),
            nullable=False,
            server_default="high",
        ),
    )
    op.add_column(
        "autodiscovery_rules",
        sa.Column(
            "security_scan_skip_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "autodiscovery_rules",
        sa.Column("ai_summary_mode", sa.String(20), nullable=False, server_default="default"),
    )
    op.add_column(
        "autodiscovery_rules",
        sa.Column("ai_summary_context", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("autodiscovery_rules", "ai_summary_context")
    op.drop_column("autodiscovery_rules", "ai_summary_mode")
    op.drop_column("autodiscovery_rules", "security_scan_skip_rules")
    op.drop_column("autodiscovery_rules", "security_scan_severity_threshold")
    op.drop_column("autodiscovery_rules", "security_scan_engine")
    op.drop_column("autodiscovery_rules", "security_scan_enforcement")
