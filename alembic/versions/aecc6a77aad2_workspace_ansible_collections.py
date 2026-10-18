"""workspace-declared Ansible collections (#1931)

Revision ID: aecc6a77aad2
Revises: f08ad84dd25b
Create Date: 2026-10-18

`ansible-core` ships no collections, so a playbook beyond the builtins depends
on content fetched at run time. Declaring that on the workspace makes it a
governed, auditable, fleet-settable supply-chain declaration rather than
whatever a repository happens to commit.

Expand-only: a nullable-free JSONB column with a server default, so an older
replica serving traffic during a rolling upgrade reads `[]` and behaves exactly
as it does today.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "aecc6a77aad2"
down_revision = "f08ad84dd25b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("workspaces", "autodiscovery_rules"):
        op.add_column(
            table,
            sa.Column(
                "ansible_collections",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default="[]",
            ),
        )


def downgrade() -> None:
    for table in ("workspaces", "autodiscovery_rules"):
        op.drop_column(table, "ansible_collections")
