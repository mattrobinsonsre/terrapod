"""Whether a Pulumi workspace binds its update to the approved preview (#1553).

Pure expand: one boolean column with a server default of false, so every existing
row gets the new default behaviour with no backfill, and an older replica
mid-rolling-upgrade reads and writes workspaces happily without knowing it exists.

False is the default on purpose. Binding means `preview --save-plan` then
`up --plan`, which rests on Pulumi's update plans — still experimental upstream.
Unbound, the preview saves nothing and the update is a plain `pulumi up`, as
Pulumi is normally run. Terraform workspaces ignore the column; the API refuses
`true` on them.

Revision ID: 0bf0f9292aa8
Revises: 697c42296453
"""

import sqlalchemy as sa

from alembic import op

revision = "0bf0f9292aa8"
down_revision = "697c42296453"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("pulumi_bind_plan", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "pulumi_bind_plan")
