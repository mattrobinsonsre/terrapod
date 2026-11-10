"""runs.phase_profiles: the resource profile kept per phase

Revision ID: 9c864f64ed11
Revises: 2e06ea7f06e4
Create Date: 2026-11-10

A composite run is two runner Jobs -- an apply, then a configure -- driven by
one Run row. Both report their resource profile to the same three scalar
columns, so the configure's peak memory and CPU overwrote the apply's and an
operator sizing a runner read the wrong figure, on exactly the runs that are
hardest to size.

One JSONB column holding `{phase: {peak_memory_bytes, peak_cpu_usec,
exit_code}}`. The existing `peak_memory_bytes`, `peak_cpu_usec` and
`runner_exit_code` columns are UNTOUCHED and keep meaning "the last phase to
report", so every existing reader behaves exactly as before.

Expand-only: one nullable column, no backfill. A run that predates this has
NULL, which the serialiser emits as an empty object -- the honest answer, since
its per-phase figures were never recorded and cannot be reconstructed from the
scalars.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "9c864f64ed11"
down_revision = "2e06ea7f06e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("phase_profiles", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("runs", "phase_profiles")
