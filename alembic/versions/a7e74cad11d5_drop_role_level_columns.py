"""drop role permission-level columns — capabilities are the only stored grant (#585)

The hierarchical permission *levels* (workspace/pool/registry/catalog_permission)
are no longer persisted: a role's grant is its ``capabilities`` set (back-filled
by e31a11e302fe and written by create/update). The levels remain only as an
authoring shorthand (expanded into capabilities on write) and a derived summary
computed on read — so storing them was a redundant second source of truth that
could drift from and contradict the enforced capabilities. Drop the columns.

Revision ID: a7e74cad11d5
Revises: e31a11e302fe
"""

import sqlalchemy as sa
from alembic import op

from terrapod.auth.capabilities import summarize_capabilities

revision = "a7e74cad11d5"
down_revision = "e31a11e302fe"
branch_labels = None
depends_on = None

_COLS = ("workspace_permission", "pool_permission", "registry_permission", "catalog_permission")


def upgrade() -> None:
    for col in _COLS:
        op.drop_column("roles", col)


def downgrade() -> None:
    # Re-add the columns and best-effort backfill from the capability summary
    # (lossy: a granular set with no matching preset restores as the literal
    # "custom", and the exact original preset choice is not recoverable).
    # ``capabilities`` is left intact here, so a single down/up round-trip at
    # THIS revision preserves the grant. The genuinely lossy case is downgrading
    # further past e31a11e302fe (which drops ``capabilities``) and then
    # re-upgrading — see that revision's downgrade() note.
    op.add_column(
        "roles",
        sa.Column("workspace_permission", sa.String(20), nullable=False, server_default="read"),
    )
    op.add_column(
        "roles", sa.Column("pool_permission", sa.String(20), nullable=False, server_default="read")
    )
    op.add_column(
        "roles",
        sa.Column("registry_permission", sa.String(20), nullable=False, server_default="read"),
    )
    op.add_column(
        "roles",
        sa.Column("catalog_permission", sa.String(20), nullable=False, server_default="none"),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT name, capabilities FROM roles")).fetchall()
    for r in rows:
        s = summarize_capabilities(r.capabilities or [])
        bind.execute(
            sa.text(
                "UPDATE roles SET workspace_permission = :w, pool_permission = :p, "
                "registry_permission = :rg, catalog_permission = :c WHERE name = :n"
            ),
            {
                "w": s["workspace_permission"],
                "p": s["pool_permission"],
                "rg": s["registry_permission"],
                "c": s["catalog_permission"],
                "n": r.name,
            },
        )
