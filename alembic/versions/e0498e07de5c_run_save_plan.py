"""Saved-plan runs: `terraform plan -out=FILE` / `terraform apply FILE`

Revision ID: e0498e07de5c
Revises: 611f0d7f501f
Create Date: 2026-09-30

`terraform plan -out=FILE` asks for a run whose apply is DEFERRED: it plans
immediately, but does not take the workspace's single apply slot until the
operator later runs `terraform apply FILE` and confirms it. That is the whole
point of holding a plan file, and it is the one thing an ordinary
awaiting-confirmation run does not do -- Terrapod's `planned` runs contend for
the workspace from the moment they plan.

go-tfe sends it as `save-plan` on run create and reads it back on the run
(v1.106.0, `run.go`). Terrapod read eleven attributes off that body and this was
not one of them, so the flag was accepted and ignored: the documented CLI
workflow appeared to work and quietly held the workspace (#1903).

Expand-only: one nullable-free boolean defaulting false, so every existing run
reads as an ordinary run, which is what it is.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "e0498e07de5c"
down_revision: str | None = "611f0d7f501f"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("save_plan", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("runs", "save_plan")
