"""ansible inventory: settings, hosts, groups, memberships and variables (#1967, #1968)

Eight new tables, nothing touched. Expand-only in the strongest sense: a replica
running the previous release neither reads nor writes any of them, so a rolling
upgrade is unaffected in both directions.

**One table per structure ansible's inventory actually has.** Group membership
and group nesting are many-to-many, and variables hang off a host, a group or
`all`, so each is its own table rather than a JSONB field on something else.
That is what lets a second concern contribute to a group it does not own, and it
is what makes `group_vars` and `[group:children]` expressible at all.

**Nothing is created for a workspace that does not use this.** There is no row
anywhere until something declares a host or binds a repository, so a deployment
running only terraform/tofu carries zero rows here -- which is how "those users
pay nothing" is delivered now that the engine on/off switch is withdrawn (#1986)
-- by data rather than by a flag an operator could set wrongly.

**The composite foreign keys are the point, not boilerplate.** Each link is
`(workspace_id, <parent>_id)` against a `UNIQUE (workspace_id, id)` on the
parent, so a membership spanning two workspaces is impossible at the database
level rather than something application code has to remember. They also give
every per-workspace query and ceiling one index to use.

**The three `*_vars.value` columns are `EncryptedText`.** They are created as
plain TEXT here because that is what the type is at rest; the encryption is
applied by the column type in the model, and all three are registered in
`crypto/columns.py::ENCRYPTED_COLUMNS` so `cli.encryption_migrate` visits them
and a DEK rotation re-keys them.

Revision ID: 7f1b648caf08
Revises: 165edbf3177c
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7f1b648caf08"
down_revision: str | None = "165edbf3177c"
branch_labels: str | None = None
depends_on: str | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _var_table(name: str, parent: str, parent_col: str) -> None:
    """One of the three variable tables. They differ only in their parent.

    Written once because they are the same structure three times over -- and
    because a copied block is where the fourth one would quietly diverge.
    `inventory_global_vars` is NOT built here: its parent is the workspace
    rather than an inventory row, so it has no composite key and a different
    unique constraint.
    """
    op.create_table(
        name,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(parent_col, postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("structured", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", parent_col],
            [f"{parent}.workspace_id", f"{parent}.id"],
            ondelete="CASCADE",
            name=f"fk_{name}_{parent_col.removesuffix('_id')}",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            parent_col, "key", name=f"uq_{name}_{parent_col.removesuffix('_id')}_key"
        ),
    )
    op.create_index(f"ix_{name}_workspace_id", name, ["workspace_id"])
    op.create_index(f"ix_{name}_{parent_col}", name, [parent_col])


def _named_entity(name: str) -> None:
    """A host or a group: a workspace-scoped name, and nothing else.

    The second unique constraint is what the composite foreign keys point at.
    Postgres requires a unique index on the referenced columns, so without it
    every link below fails to create -- which is why it is here rather than
    looking like a redundant pair with the primary key.
    """
    op.create_table(
        name,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name=f"uq_{name}_workspace_name"),
        sa.UniqueConstraint("workspace_id", "id", name=f"uq_{name}_workspace_id"),
    )
    op.create_index(f"ix_{name}_workspace_id", name, ["workspace_id"])


def upgrade() -> None:
    op.create_table(
        "inventory_settings",
        # The workspace IS the key: one inventory per workspace, so there is no
        # surrogate id and no way to create a second row.
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("include_platform", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("vcs_connection_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("repo_url", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("branch", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("working_directory", sa.String(length=500), nullable=False, server_default=""),
        sa.Column(
            "ignore_paths",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        # SET NULL rather than CASCADE: deleting a VCS connection must not
        # silently delete a workspace's whole inventory configuration along with
        # it. The CHECK then makes the resulting state visible -- a repo with no
        # connection -- rather than letting it look configured.
        sa.ForeignKeyConstraint(["vcs_connection_id"], ["vcs_connections.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("workspace_id"),
        sa.CheckConstraint(
            "vcs_connection_id IS NOT NULL OR repo_url = ''",
            name="ck_inventory_settings_repo_needs_connection",
        ),
    )
    op.create_index(
        "ix_inventory_settings_vcs_connection_id", "inventory_settings", ["vcs_connection_id"]
    )

    _named_entity("inventory_hosts")
    _named_entity("inventory_groups")

    op.create_table(
        "inventory_host_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("host_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "host_id"],
            ["inventory_hosts.workspace_id", "inventory_hosts.id"],
            ondelete="CASCADE",
            name="fk_inventory_host_groups_host",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "group_id"],
            ["inventory_groups.workspace_id", "inventory_groups.id"],
            ondelete="CASCADE",
            name="fk_inventory_host_groups_group",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host_id", "group_id", name="uq_inventory_host_groups_pair"),
    )
    op.create_index(
        "ix_inventory_host_groups_workspace_id", "inventory_host_groups", ["workspace_id"]
    )
    op.create_index("ix_inventory_host_groups_group_id", "inventory_host_groups", ["group_id"])

    op.create_table(
        "inventory_group_children",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("child_group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "parent_group_id"],
            ["inventory_groups.workspace_id", "inventory_groups.id"],
            ondelete="CASCADE",
            name="fk_inventory_group_children_parent",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "child_group_id"],
            ["inventory_groups.workspace_id", "inventory_groups.id"],
            ondelete="CASCADE",
            name="fk_inventory_group_children_child",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "parent_group_id", "child_group_id", name="uq_inventory_group_children_pair"
        ),
        # The one-step cycle. A longer one needs a graph walk, which is the
        # service's job because a CHECK cannot do it.
        sa.CheckConstraint(
            "parent_group_id <> child_group_id", name="ck_inventory_group_children_not_self"
        ),
    )
    op.create_index(
        "ix_inventory_group_children_workspace_id", "inventory_group_children", ["workspace_id"]
    )
    op.create_index(
        "ix_inventory_group_children_child_group_id", "inventory_group_children", ["child_group_id"]
    )

    _var_table("inventory_host_vars", "inventory_hosts", "host_id")
    _var_table("inventory_group_vars", "inventory_groups", "group_id")

    op.create_table(
        "inventory_global_vars",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("structured", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_timestamps(),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "key", name="uq_inventory_global_vars_workspace_key"),
    )
    op.create_index(
        "ix_inventory_global_vars_workspace_id", "inventory_global_vars", ["workspace_id"]
    )


def downgrade() -> None:
    # Reverse creation order: the links and variables reference the hosts and
    # groups, so they go first.
    for table in (
        "inventory_global_vars",
        "inventory_group_vars",
        "inventory_host_vars",
        "inventory_group_children",
        "inventory_host_groups",
        "inventory_groups",
        "inventory_hosts",
        "inventory_settings",
    ):
        op.drop_table(table)
