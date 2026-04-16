"""Add agent pool RBAC (labels, owner_email, pool_permission).

Adds label-based RBAC to agent pools, mirroring the existing workspace
and registry RBAC patterns. Pools get labels + owner_email for permission
resolution, and roles get pool_permission for fine-grained access control.

Existing pools default to access=everyone so they remain visible to all
users after the migration.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "ad9e14fa1469"
down_revision = "cc6a19db3ee3"


def upgrade() -> None:
    # AgentPool additions
    op.add_column("agent_pools", sa.Column("labels", JSONB, nullable=False, server_default="{}"))
    op.add_column("agent_pools", sa.Column("owner_email", sa.String(255), nullable=True))

    # Role addition
    op.add_column(
        "roles",
        sa.Column("pool_permission", sa.String(20), nullable=False, server_default="read"),
    )

    # Backward compat: existing pools get access=everyone so they remain
    # visible to all users after upgrade. Newly created pools get {} (empty)
    # by default — admins must set labels explicitly for RBAC visibility.
    op.execute('UPDATE agent_pools SET labels = \'{"access": "everyone"}\'')


def downgrade() -> None:
    op.drop_column("roles", "pool_permission")
    op.drop_column("agent_pools", "owner_email")
    op.drop_column("agent_pools", "labels")
