"""a workspace may relax SSH host-key verification for its configure runs

Revision ID: aac35aebdea1
Revises: 0b0a816c5d42
Create Date: 2026-10-30

One column on `workspaces` and the matching one on `autodiscovery_rules`, so a
rule templates it like every other configure setting (#2318).

**It defaults to TRUE, which is verification ON, and that is already the
behaviour rather than a new restriction.** Measured against the published
execution-environment image: ansible's own `HOST_KEY_CHECKING` defaults to
`True`, the configure phase forces nothing about it, and no `known_hosts` file
ships -- so first contact with an unseen host fails closed today with
`Host key verification failed`. What was missing was not the default but any way
to express an exception to it, which is what this column and the machine
credential's new `known_hosts` field supply between them.

The column is deliberately NOT the mechanism. Turning it off makes the resolver
emit `ansible_ssh_common_args` as a variable on `all`, so an inventory host
variable still beats it -- measured: a host variable REPLACES a group `all`
variable rather than merging, so a host can re-enable verification over a
workspace-level relaxation. Forcing `ANSIBLE_HOST_KEY_CHECKING` into the
environment instead would have overridden the operator's own `ansible.cfg`,
which this project reserves for non-negotiables, and would have sat ABOVE the
per-host override rather than below it.

EXPAND-ONLY, so it is safe under a rolling upgrade: two columns added with a
server default, nothing dropped, renamed or retyped. Every existing workspace
and rule gets `true` and therefore behaves exactly as it does today, and an
older replica serving traffic alongside the new one simply never reads them.
"""

import sqlalchemy as sa
from alembic import op

revision = "aac35aebdea1"
down_revision = "0b0a816c5d42"
branch_labels = None
depends_on = None

#: Named rather than counted, because a number in a comment has nothing
#: enforcing it. The rule mirrors the workspace so a rule can template it.
_TABLES = ("workspaces", "autodiscovery_rules")

_COLUMN = "ansible_host_key_checking"


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                _COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )


def downgrade() -> None:
    # A real downgrade: the column holds a workspace's own declaration and
    # nothing else depends on it, so dropping it returns every workspace to
    # verification always on -- which is what it did before this revision, and
    # is the safe direction rather than the lossy one.
    for table in reversed(_TABLES):
        op.drop_column(table, _COLUMN)
