"""workspaces/rules/runs.configure_parallelism: the configure phase's own concurrency

Revision ID: 4393ae805ce2
Revises: 57d631bbd487
Create Date: 2026-11-11

One `parallelism` served every phase, while the resource allocation it converts
into load has been split per phase since #2475. `ee_resource_cpu`/`_memory` exist
separately from `resource_cpu`/`_memory` because "one pair cannot size both" --
and `parallelism` describes itself as the knob turning "the allocation above"
into actual load, which is now two allocations in two containers (#2499).

The axis is plan/apply vs configure, NOT engine: terraform's `-parallelism` and
Pulumi's `--parallel` are the same question in the same container, so #1431's
per-engine unification stands. Ansible's `forks` runs in the EE sidecar and is
bounded by that container's own CPU and memory.

Expand-only: one NULLABLE column per table, no backfill. `NULL` means inherit
`parallelism`, so every existing workspace, rule and run behaves exactly as
before. Deliberately not `0` as the inherit marker -- `validate_parallelism`
rejects `0` because terraform reads `-parallelism=0` as "no limit".

Table names are the REAL ones: the rule's class is `AutodiscoveryRule` and its
table is `autodiscovery_rules`, while a sibling class owns
`module_autodiscovery_rules` -- so the wrong guess is a plausible one, and no
pytest tier would catch it, because the integration tier builds its tables from
`Base.metadata` rather than by running this chain.
"""

import sqlalchemy as sa
from alembic import op

revision = "4393ae805ce2"
down_revision = "57d631bbd487"
branch_labels = None
depends_on = None

_TABLES = ("workspaces", "autodiscovery_rules", "runs")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("configure_parallelism", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "configure_parallelism")
