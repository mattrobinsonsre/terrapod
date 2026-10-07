"""a per-workspace ansible-core version, defaulting to the deployment's (#2010)

Expand-only: two nullable-equivalent columns with an empty server default, so a
replica running the previous release is unaffected -- it neither reads nor writes
them, and an empty value is exactly what the new code reads as "inherit the
deployment default".

The server_default is a literal version, following `engine_version` exactly --
including that a later bump moves only the server_default and never existing
rows, the way `3214ad466305` does. Consistency with the engine version is the
point: two version fields on one workspace behaving differently is a trap. It is
also the safer shape, because an ansible-core minor changes behaviour as well as
fixing bugs, so a `helm upgrade` must not move a workspace onto one. Bulk update
is how a fleet is moved deliberately.

Every existing row is therefore stamped with the current default as the column is
added, which is the same thing "new rows get the default" means when the column
is new.

Revision ID: 165edbf3177c
Revises: 27f4b5b603d0
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "165edbf3177c"
down_revision: str | None = "27f4b5b603d0"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("ansible_version", sa.String(length=20), nullable=False, server_default="2.21.5"),
    )
    op.add_column(
        "autodiscovery_rules",
        sa.Column("ansible_version", sa.String(length=20), nullable=False, server_default="2.21.5"),
    )


def downgrade() -> None:
    op.drop_column("autodiscovery_rules", "ansible_version")
    op.drop_column("workspaces", "ansible_version")
