"""grant workspace:onboard to run:plan roles (#824)

Backfills the new ``workspace:onboard`` capability (#824) onto every existing
role that already holds ``run:plan`` — the same trust class (both cause the
workspace's pool identity to touch the cloud; onboarding runs read-only
discovery with those creds). New roles pick it up automatically via the plan
preset in ``terrapod.auth.capabilities``; this migration exists because stored
roles carry a frozen capability snapshot that won't retroactively gain a new
preset member.

It's a dedicated token, so an operator can revoke it from any single role
afterwards. Enforcement (the onboarding endpoints) lands in a later phase, so
this migration is a no-op on effective access until then.

Revision ID: ca34d7deb54d
Revises: 5456426d7dd8
"""

import json

import sqlalchemy as sa

from alembic import op
from terrapod.auth.capabilities import RUN_PLAN, WORKSPACE_ONBOARD

revision = "ca34d7deb54d"
down_revision = "5456426d7dd8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT name, capabilities FROM roles")).fetchall()
    for r in rows:
        caps = list(r.capabilities or [])
        if RUN_PLAN in caps and WORKSPACE_ONBOARD not in caps:
            new = sorted(set(caps) | {WORKSPACE_ONBOARD})
            bind.execute(
                sa.text("UPDATE roles SET capabilities = CAST(:caps AS JSONB) WHERE name = :name"),
                {"caps": json.dumps(new), "name": r.name},
            )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT name, capabilities FROM roles")).fetchall()
    for r in rows:
        caps = list(r.capabilities or [])
        if WORKSPACE_ONBOARD in caps:
            new = sorted(set(caps) - {WORKSPACE_ONBOARD})
            bind.execute(
                sa.text("UPDATE roles SET capabilities = CAST(:caps AS JSONB) WHERE name = :name"),
                {"caps": json.dumps(new), "name": r.name},
            )
