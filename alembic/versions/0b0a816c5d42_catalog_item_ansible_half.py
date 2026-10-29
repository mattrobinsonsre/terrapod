"""a catalog item may provision the Ansible half of a workspace

Revision ID: 0b0a816c5d42
Revises: d70160a15ce0
Create Date: 2026-10-29

Four columns on `catalog_items` so an item can provision a workspace that is
primarily Ansible, carrying only as much Terraform as it takes to declare the
inventory (#2019). They are workspace-settings templating, the same shape as
the `execution_mode` and `engine_version` the catalog already sets:

    configure_enabled   whether the provisioned workspace has a configure
    ansible_directory   the subtree copied out of the module, and the
                        `config_directory` the workspace is given
    default_playbook    templated onto the workspace
    default_limit       templated onto the workspace

`configure_enabled` says WHETHER and `ansible_directory` says WHERE, which is
deliberate rather than tidy. #1929 shipped `config_directory` nullable with
`None` carrying "no configure operation", and #1966 corrected it precisely
because that made one field answer two questions; carrying it in this path's
nullability would reintroduce the same defect one table over.

EXPAND-ONLY, which is what makes it safe under a rolling upgrade: four columns
added with a server default, nothing dropped, renamed or retyped. Every existing
item gets `configure_enabled = false` and three empty strings, so it provisions
exactly what it provisions today and an older replica serving traffic alongside
the new one simply never reads them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0b0a816c5d42"
down_revision = "d70160a15ce0"
branch_labels = None
depends_on = None

_TABLE = "catalog_items"

#: The three string columns, which differ from the boolean only in type and
#: default. Named rather than counted, because a number in a comment has
#: nothing enforcing it.
_STRING_COLUMNS = ("ansible_directory", "default_playbook", "default_limit")


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "configure_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    for name in _STRING_COLUMNS:
        op.add_column(
            _TABLE,
            sa.Column(name, sa.String(length=255), nullable=False, server_default=""),
        )


def downgrade() -> None:
    # A real downgrade: these columns hold an item's own declaration and nothing
    # else depends on them, so dropping them returns an item to provisioning the
    # Terraform half alone -- which is what it did before this revision. A
    # workspace already provisioned keeps its own settings, because those live on
    # `workspaces` and were copied at provisioning rather than read through.
    for name in reversed(_STRING_COLUMNS):
        op.drop_column(_TABLE, name)
    op.drop_column(_TABLE, "configure_enabled")
