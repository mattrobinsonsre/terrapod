"""Role.registry_permission — dedicated registry RBAC level (modules + providers).

Previously a role's registry permission was DERIVED from its
`workspace_permission` (read/plan -> read, write -> write, admin -> admin),
so a registry-write role implicitly carried workspace write too. This adds a
first-class `registry_permission` (read/write/admin) so registry access is
independent of workspace access — mirroring `pool_permission`.

Back-compat: every existing role is backfilled from its current
`workspace_permission` via the same mapping that was applied at resolution
time, so no role's effective registry access changes on upgrade.

Revision ID: 2ad40c9b26b6
Revises: 3fbcb2885b93
"""

import sqlalchemy as sa
from alembic import op

revision = "2ad40c9b26b6"
down_revision = "3fbcb2885b93"


def upgrade() -> None:
    # New rows default to "read" (matching pool_permission). Existing rows are
    # backfilled below to preserve their current effective registry access.
    op.add_column(
        "roles",
        sa.Column(
            "registry_permission",
            sa.String(20),
            nullable=False,
            server_default="read",
        ),
    )
    # Preserve current behaviour: registry level was workspace_permission mapped
    # read/plan -> read, write -> write, admin -> admin.
    op.execute(
        """
        UPDATE roles SET registry_permission = CASE workspace_permission
            WHEN 'admin' THEN 'admin'
            WHEN 'write' THEN 'write'
            ELSE 'read'
        END
        """
    )


def downgrade() -> None:
    op.drop_column("roles", "registry_permission")
