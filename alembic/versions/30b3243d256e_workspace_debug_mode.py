"""Per-workspace runner debug mode (#1764).

A runner Job's pod runs `restartPolicy: Never`, so a failed run's container is
already terminated by the time anyone looks — and you cannot `kubectl exec`
into a terminated container at any TTL. That is precisely when an operator
wants to get inside: to see the credentials that did not work, the DNS or
`hostAliases` that did not resolve, the mounts that were not there, the egress
that was blocked.

`runners.ttlSecondsAfterFinished` does not help. It is per-deployment, so it
cannot be raised for one workspace, and it governs how long a *finished* Job is
kept rather than whether the process is still running.

So this is per workspace and opt-in. With it on, the orchestrator reports the
failure exactly as it always does and only then holds the container open; the
run is failed and final from Terrapod's side, and the pod is simply still there.

Defaults false, so nothing changes for an existing workspace. How LONG a pod is
held is deliberately not stored here: it is `runners.debugLingerSeconds` on the
deployment, because the pod keeps the run's auth token and its decrypted
`terraform.tfvars.json` for that whole window. A workspace admin can ask for a
debug pod; only the operator decides how long one may survive.

Revision ID: 30b3243d256e
Revises: 87560d7db332
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "30b3243d256e"
down_revision: str | Sequence[str] | None = "87560d7db332"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("debug_mode", sa.Boolean(), nullable=False, server_default="false"),
    )
    # Templated onto every workspace an autodiscovery rule materialises, per
    # the surface-parity rule (#1763).
    op.add_column(
        "autodiscovery_rules",
        sa.Column("debug_mode", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("autodiscovery_rules", "debug_mode")
    op.drop_column("workspaces", "debug_mode")
