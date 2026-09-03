"""role: allow_all, an explicit estate-wide grant

Before this, a custom role could not grant on every resource. Label and name
rules are exact-match only (no wildcards), so covering the estate meant putting
a shared label on every workspace and allowing on that — which fails in the
dangerous direction: a workspace created without the label silently falls
outside the role, so an operator believes they have coverage they do not have.

The alternative of treating "*" as a wildcard value was rejected: a resource
can legitimately be NAMED "*", and a magic string in an RBAC allow-list is how
a deployment ends up with coverage nobody intended. An explicit boolean cannot
be granted by accident.

Additive: NOT NULL with server_default false, so every existing role keeps
exactly the reach it had.

Revision ID: 8c02c0a6b39b
Revises: b7c41e05d9a2
"""

import sqlalchemy as sa
from alembic import op

revision: str = "8c02c0a6b39b"
down_revision: str | None = "b7c41e05d9a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "roles",
        sa.Column("allow_all", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("roles", "allow_all")
