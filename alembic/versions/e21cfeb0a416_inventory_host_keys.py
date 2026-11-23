"""approved ssh host keys as an inventory structure (#2592)

One new table, nothing dropped or retyped, and no replica of the previous
release reads or writes it -- expand-only in the strongest sense, so a rolling
upgrade is unaffected in both directions.

**The eighth structure ansible's inventory has, not a host variable.** Two
measured reasons it is a table rather than a reserved variable name. There is no
reserved-name guard for inventory variables, so a magic name would need one
built and applied at every write path including create. And a host variable
lands in ansible's own variable namespace, where `setup` gathers
`ansible_ssh_host_key_{rsa,ecdsa,ed25519}_public` from the node and ansible's
`vars/clean.py` carves exactly those names out of its restricted-key stripping
so that they survive -- which would put a DECLARED trust anchor beside a
GATHERED one with ansible's precedence rules deciding which a play sees. A trust
anchor is the last thing that should be shadowable by a fact.

**One row per `known_hosts` LINE.** `marker` is per line rather than per
algorithm -- ansible's own `known_hosts` module documents the field that way and
`x/crypto/ssh/knownhosts` keys its revoked set on the marshalled KEY -- and a
host legitimately holds two keys of one type while it is being rotated. So the
unique constraint is `(host_id, key_type, public_key)` and deliberately NOT
`(host_id, key_type)`, which is what the sibling variable tables'
`(host_id, key)` transliterates to and which would forbid the one state this
table exists to hold.

**`public_key` is plain `Text`, not encrypted**, unlike the sibling variable
tables' `value`. A host's public key is public, and registering it with the
run's log redactor would replace the one value an operator needs to read when a
key does not match -- the same reasoning that keeps `known_hosts` out of
`MACHINE_SECRET_FIELDS`. It is therefore absent from
`crypto/columns.py::ENCRYPTED_COLUMNS` on purpose rather than by omission, and
no DEK rotation has anything to re-key here.

The composite foreign key is the siblings' own shape, and it is what makes a key
for a host in a different workspace structurally impossible rather than merely
refused in a service layer.

Revision ID: e21cfeb0a416
Revises: aa2412ea796d
Create Date: 2026-11-23

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e21cfeb0a416"
down_revision = "aa2412ea796d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inventory_host_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("host_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_type", sa.String(length=64), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("marker", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        # Composite, against `inventory_hosts`' own `(workspace_id, id)` unique
        # constraint -- which is what that otherwise-odd constraint exists for.
        sa.ForeignKeyConstraint(
            ["workspace_id", "host_id"],
            ["inventory_hosts.workspace_id", "inventory_hosts.id"],
            ondelete="CASCADE",
            name="fk_inventory_host_keys_host",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "host_id",
            "key_type",
            "public_key",
            name="uq_inventory_host_keys_host_type_key",
        ),
    )
    op.create_index(
        "ix_inventory_host_keys_workspace_id",
        "inventory_host_keys",
        ["workspace_id"],
    )
    op.create_index(
        "ix_inventory_host_keys_host_id",
        "inventory_host_keys",
        ["host_id"],
    )


def downgrade() -> None:
    # A real downgrade, and a complete one: the table is new, so dropping it
    # cannot strand a row anything else references. No `schema_contractions`
    # marker is owed either -- nothing existing is removed, so no older replica
    # can be reading something this takes away.
    op.drop_index("ix_inventory_host_keys_host_id", table_name="inventory_host_keys")
    op.drop_index("ix_inventory_host_keys_workspace_id", table_name="inventory_host_keys")
    op.drop_table("inventory_host_keys")
