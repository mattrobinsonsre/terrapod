"""workspace playbooks: the allow-list the `playbook` run option is bounded by

Revision ID: 26175f851307
Revises: f9bb1c338a1c
Create Date: 2026-11-12

`default_playbook` is a DEFAULT -- a run may name any playbook instead, typed
from memory, with a mistyped name failing only once the run has started. This is
the set a configure may choose from, so a playbook that must never run against
this workspace (a node drain, a database upgrade) is a control rather than a
convention.

Expand-only: a new non-null JSONB column with a server default of `[]`, which is
"no restriction" -- so every existing workspace keeps today's free-text
behaviour and no backfill is needed. It drops nothing, so it records no
`schema_contractions` row.

Not on `catalog_items`, and that is a decision rather than an omission: an
item's Ansible block templates `default_playbook` onto the workspace it
provisions, and whether a catalog AUTHOR should also bound which playbooks a
provisioned workspace may run is a question about catalog authoring that #2527
does not ask. An empty list on the provisioned workspace is today's behaviour,
so nothing is lost by leaving it; the operator can set one on the workspace.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "26175f851307"
down_revision = "f9bb1c338a1c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("workspaces", "autodiscovery_rules"):
        op.add_column(
            table,
            sa.Column(
                "playbooks",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )


def downgrade() -> None:
    for table in ("autodiscovery_rules", "workspaces"):
        op.drop_column(table, "playbooks")
