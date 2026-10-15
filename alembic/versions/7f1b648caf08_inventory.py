"""ansible inventory: hosts, groups, memberships and variables (#1967, #1968, #1929)

Seven new tables and eight new workspace columns, nothing dropped or retyped.
Expand-only in the strongest sense: a replica running the previous release
neither reads nor writes any of them, so a rolling upgrade is unaffected in
both directions.

The inventory's roots and defaults are **workspace columns**, not a table
(#1929): the settings row would have carried its own VCS binding, and a
workspace has exactly one (#1407 point 11), so a differing ROOT does not
require a differing BINDING. With the binding gone the table held two columns,
and those fold onto the workspace too.

**One table per structure ansible's inventory actually has.** Group membership
and group nesting are many-to-many, and variables hang off a host, a group or
`all`, so each is its own table rather than a JSONB field on something else.
That is what lets a second concern contribute to a group it does not own, and it
is what makes `group_vars` and `[group:children]` expressible at all.

**The workspace DECLARES its operations; a path does not carry that meaning**
(#1966). `plan_apply_enabled` defaults TRUE and `configure_enabled` defaults
FALSE, so every workspace that exists keeps exactly today's behaviour with no
data step and configure is opt-in. Two independent booleans rather than an enum
or a delimited string, because a value that must be parsed and kept in step
across the API, the provider, MCP and the UI gets mistyped into a third meaning
eventually, and two booleans cannot be.

`config_directory` is therefore an ordinary non-nullable path with `''` meaning
the configuration version's root -- `working_directory`'s exact shape. #1929
shipped it nullable with NULL meaning "no configure operation", which made one
column carry both a path and a capability; the declaration now lives in the
boolean that is actually about capability, and the path defaults to the root of
the tree it is relative to, as a path should.

**Nothing is created for a workspace that does not use this.** There is no row
anywhere until something declares a host, so a deployment running only
terraform/tofu carries zero rows here -- which is how "those users pay nothing"
is delivered now that the engine on/off switch is withdrawn (#1986): by data
rather than by a flag an operator could set wrongly.

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
    # The inventory's roots and defaults are WORKSPACE columns (#1929). There is
    # no `inventory_settings` table: it carried its own VCS binding, and a
    # workspace has one binding (#1407 point 11) -- Terraform and Ansible in
    # different repositories is not supported, so a differing ROOT does not
    # require a differing BINDING. With that gone the table held two columns, and
    # those fold onto the workspace as well.
    op.add_column(
        "workspaces",
        # An ordinary path: non-nullable, '' meaning the configuration version's
        # root, exactly like `working_directory`. It carries NO capability
        # meaning -- whether this workspace HAS a configure operation is
        # `configure_enabled` below (#1966). #1929 shipped this nullable with
        # NULL standing for "no configure operation", which made one column mean
        # both a location and a capability; a path's natural default is the root
        # of the tree it is relative to.
        sa.Column("config_directory", sa.String(length=500), nullable=False, server_default=""),
    )
    op.add_column(
        "workspaces",
        # An ORDERED list, each entry a file or a directory. Order is precedence:
        # `inventory` in `ansible.cfg` is `type: pathlist` and a later source
        # wins per variable. A list rather than one path because a source may be
        # a FILE -- pointing at the containing directory instead MERGES sibling
        # environments, so a staging workspace would hold production hosts.
        sa.Column(
            "inventory_paths",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "workspaces",
        # The playbook a configure runs when the run names none. Overridden per
        # run exactly as `vcs-ref` overrides `vcs_branch` -- there is always a
        # default and nobody is prompted for it.
        sa.Column("default_playbook", sa.String(length=500), nullable=False, server_default=""),
    )
    op.add_column(
        "workspaces",
        # Empty means the whole inventory. `--limit` is a run option in the shape
        # of `-target`, and this is its workspace default.
        sa.Column("default_limit", sa.String(length=500), nullable=False, server_default=""),
    )
    op.add_column(
        "workspaces",
        # Whether the declared rows take part in resolution. False resolves the
        # git source alone, which is how an operator moves a repository's
        # inventory in before declaring anything.
        sa.Column(
            "inventory_include_platform",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "workspaces",
        # Ansible reads a directory as ONE source, taking every file in it, so
        # this is the only way to leave a file out of a directory source.
        sa.Column(
            "inventory_ignore_paths",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # The DECLARATION: which operations this workspace has (#1966). Two
    # independent booleans, not an enum and not a delimited string -- the user's
    # reason is that a string has to be got right, and these cannot be mistyped
    # into a third meaning.
    #
    # The defaults ARE the compatibility story: every existing row keeps today's
    # behaviour without a data step, and configure is opt-in. Both false is
    # refused at the write boundary rather than stored, because a workspace that
    # can do nothing is not a state anyone wants and accepting it would make
    # every consumer -- dispatcher, reconciler, autodiscovery, UI -- carry a
    # "nothing to do" branch. Not a CHECK constraint: the refusal belongs where
    # it can name the field and answer 422, and a constraint violation here
    # would surface as a bare 409 naming neither.
    op.add_column(
        "workspaces",
        sa.Column("plan_apply_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "workspaces",
        sa.Column("configure_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # And the same eight on autodiscovery rules, which materialise workspaces
    # from their template fields (#1929). A rule without them could only ever
    # produce plan/apply workspaces, so a fleet of configure-capable ones would
    # have to be edited one at a time after the fact. `165edbf3177c` is the
    # precedent: one column added to both tables in one migration.
    for _table in ("autodiscovery_rules",):
        op.add_column(
            _table,
            sa.Column(
                "config_directory",
                sa.String(length=500),
                nullable=False,
                server_default="",
            ),
        )
        op.add_column(
            _table,
            sa.Column(
                "plan_apply_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
        op.add_column(
            _table,
            sa.Column(
                "configure_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
        op.add_column(
            _table,
            sa.Column(
                "inventory_paths",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )
        op.add_column(
            _table,
            sa.Column(
                "default_playbook",
                sa.String(length=500),
                nullable=False,
                server_default="",
            ),
        )
        op.add_column(
            _table,
            sa.Column(
                "default_limit",
                sa.String(length=500),
                nullable=False,
                server_default="",
            ),
        )
        op.add_column(
            _table,
            sa.Column(
                "inventory_include_platform",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
        op.add_column(
            _table,
            sa.Column(
                "inventory_ignore_paths",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
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
        "ix_inventory_host_groups_workspace_id",
        "inventory_host_groups",
        ["workspace_id"],
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
            "parent_group_id <> child_group_id",
            name="ck_inventory_group_children_not_self",
        ),
    )
    op.create_index(
        "ix_inventory_group_children_workspace_id",
        "inventory_group_children",
        ["workspace_id"],
    )
    op.create_index(
        "ix_inventory_group_children_child_group_id",
        "inventory_group_children",
        ["child_group_id"],
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
        "ix_inventory_global_vars_workspace_id",
        "inventory_global_vars",
        ["workspace_id"],
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
    ):
        op.drop_table(table)

    for _table in ("autodiscovery_rules", "workspaces"):
        for _col in (
            "configure_enabled",
            "plan_apply_enabled",
            "inventory_ignore_paths",
            "inventory_include_platform",
            "default_limit",
            "default_playbook",
            "inventory_paths",
            "config_directory",
        ):
            op.drop_column(_table, _col)
