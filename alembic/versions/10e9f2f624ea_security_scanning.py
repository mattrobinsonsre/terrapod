"""security_scanning — Checkov/Trivy IaC scan config + per-run results (#1036)

Revision ID: 10e9f2f624ea
Revises: b277deafcef7
Create Date: 2026-07-24

Part 1 of the security-scanning epic (#1036): a deterministic IaC-misconfig
scan stage parallel to OPA policy sets.

- Per-workspace config columns on ``workspaces`` (enforcement / engine /
  severity threshold / skip rules). Enforcement defaults to ``advisory`` (scan
  runs + surfaces findings, never blocks) — the "valuable + non-blocking → on by
  default" stance, uniform across new and existing workspaces. Only
  ``mandatory`` (which blocks apply) is opt-in.
- ``security_scan_results`` — one row per run, the structural twin of
  ``policy_evaluations`` (snapshotted enforcement/threshold, outcome, normalized
  findings, override columns), unique on ``run_id`` for ON CONFLICT idempotency.

Additive + reversible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "10e9f2f624ea"
down_revision: str | None = "b277deafcef7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- per-workspace scan config (server defaults → existing rows = off) ---
    op.add_column(
        "workspaces",
        sa.Column(
            "security_scan_enforcement",
            sa.String(length=20),
            nullable=False,
            server_default="advisory",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "security_scan_engine",
            sa.String(length=20),
            nullable=False,
            server_default="checkov",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "security_scan_severity_threshold",
            sa.String(length=20),
            nullable=False,
            server_default="high",
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "security_scan_skip_rules",
            JSONB(),
            nullable=False,
            server_default="[]",
        ),
    )

    # --- per-run scan result (twin of policy_evaluations) ---
    op.create_table(
        "security_scan_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("engine", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("enforcement_level", sa.String(length=20), nullable=False),
        sa.Column(
            "severity_threshold",
            sa.String(length=20),
            nullable=False,
            server_default="high",
        ),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("findings", JSONB(), nullable=False, server_default="[]"),
        sa.Column("summary", JSONB(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("overridden_by", sa.String(length=255), nullable=True),
        sa.Column("overridden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("run_id", name="uq_security_scan_results_run"),
    )
    op.create_index("ix_security_scan_results_run_id", "security_scan_results", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_security_scan_results_run_id", table_name="security_scan_results")
    op.drop_table("security_scan_results")
    op.drop_column("workspaces", "security_scan_skip_rules")
    op.drop_column("workspaces", "security_scan_severity_threshold")
    op.drop_column("workspaces", "security_scan_engine")
    op.drop_column("workspaces", "security_scan_enforcement")
