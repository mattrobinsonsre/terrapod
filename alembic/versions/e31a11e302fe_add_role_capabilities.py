"""add role capabilities column + expand existing roles (#585)

Capability-based RBAC, data layer. Adds ``roles.capabilities`` (JSONB list) and
**expands every existing role row** into its explicit capability set via the
shared preset map (``terrapod.auth.capabilities.expand_preset`` — the SAME map
the roles router uses on create/update, so migration and runtime can't drift).

Faithful expansion: the capability set equals exactly what the role's levels
granted — no power added, none removed. Resolution does NOT yet read this column
(that switch is a follow-up), so this migration is a no-op on effective access.

Built-in roles (admin/audit/everyone) are not rows here; their capability sets
live in ``terrapod.auth.capabilities`` and are applied in code.

Revision ID: e31a11e302fe
Revises: 2b369cd58093
"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from terrapod.auth.capabilities import expand_preset

revision = "e31a11e302fe"
down_revision = "2b369cd58093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "roles",
        sa.Column("capabilities", JSONB(), nullable=False, server_default="[]"),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT name, workspace_permission, pool_permission, "
            "registry_permission, catalog_permission FROM roles"
        )
    ).fetchall()
    for r in rows:
        # expand_preset is total (unknown/None values contribute nothing), so an
        # unexpected legacy value never aborts the migration.
        caps = expand_preset(
            workspace_permission=r.workspace_permission,
            pool_permission=r.pool_permission,
            registry_permission=r.registry_permission,
            catalog_permission=r.catalog_permission,
        )
        bind.execute(
            sa.text("UPDATE roles SET capabilities = CAST(:caps AS JSONB) WHERE name = :name"),
            {"caps": json.dumps(caps), "name": r.name},
        )


def downgrade() -> None:
    # Lossy by nature (a customised capability set can't be represented as a
    # single level); the role's level columns are untouched, so collapsing back
    # to them is the intended behaviour.
    op.drop_column("roles", "capabilities")
