"""Rename execution_mode 'remote' to 'agent'.

Terrapod uses 'agent' execution mode (matching TFE's agent concept) for
server-side execution via agent pools. The 'remote' value was a misnomer
borrowed from TFE's remote mode (TFE-hosted workers), which Terrapod does
not support.
"""

revision = "cc6a19db3ee3"
down_revision = "3efe17078298"

from alembic import op  # noqa: E402


def upgrade() -> None:
    op.execute("UPDATE workspaces SET execution_mode = 'agent' WHERE execution_mode = 'remote'")


def downgrade() -> None:
    op.execute("UPDATE workspaces SET execution_mode = 'remote' WHERE execution_mode = 'agent'")
