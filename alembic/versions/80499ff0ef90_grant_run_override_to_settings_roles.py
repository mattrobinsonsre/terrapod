"""grant run:override to roles holding workspace:settings

Override of a soft-failed policy, scan or AI-policy gate used to be gated on
``workspace:settings``; it is now its own ``run:override`` capability in the
admin tier. Built-in admin and workspace owners gain it through the tier, but
stored roles carry a frozen capability snapshot that never gains a new tier
member on its own. Without this backfill every custom role that could override
yesterday would lose the ability on upgrade.

Expand-only: it adds one token to roles that already held the authority it
now names, so nobody's access changes. The downgrade removes the token from
every role, since the previous release rejects an unknown capability the next
time such a role is edited.

Revision ID: 80499ff0ef90
Revises: b56cc921fd47
"""

import json

import sqlalchemy as sa

from alembic import op

revision = "80499ff0ef90"
down_revision = "b56cc921fd47"
branch_labels = None
depends_on = None

# Literals, not catalogue imports: a migration must keep meaning what it meant.
_SETTINGS = "workspace:settings"
_OVERRIDE = "run:override"


def _rewrite(change) -> None:
    bind = op.get_bind()
    for r in bind.execute(sa.text("SELECT name, capabilities FROM roles")).fetchall():
        caps = list(r.capabilities or [])
        new = change(set(caps))
        if new is not None and new != sorted(caps):
            bind.execute(
                sa.text("UPDATE roles SET capabilities = CAST(:caps AS JSONB) WHERE name = :name"),
                {"caps": json.dumps(new), "name": r.name},
            )


def upgrade() -> None:
    _rewrite(lambda caps: sorted(caps | {_OVERRIDE}) if _SETTINGS in caps else None)


def downgrade() -> None:
    _rewrite(lambda caps: sorted(caps - {_OVERRIDE}) if _OVERRIDE in caps else None)
