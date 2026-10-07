"""the inventory object, its ordered sources, declared items and snapshots (#1967, #1968)

Four new tables, nothing touched. Expand-only in the strongest sense: a replica
running the previous release neither reads nor writes any of them, so a rolling
upgrade is unaffected in both directions.

**Nothing is created for a workspace that does not use this.** The `default`
inventory and its one `terraform` source are written lazily, on the first
declared item or the first read of the resolved view. A deployment that runs
only terraform/tofu therefore carries zero rows here, which is how "those users
pay nothing" is delivered now that the engine on/off switch is withdrawn
(#1986) -- by data rather than by a flag an operator could set wrongly.

`inventory_versions.produced_by_ref` is deliberately a plain string rather than
a foreign key. It records what produced a snapshot, which for a runner-produced
one is a run id -- but the thing a configure *is* has not been built yet (#1971,
#1972, #1988), and a column presuming that table's name is a guess this schema
could not take back. A string costs nothing and constrains nothing.

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


def upgrade() -> None:
    op.create_table(
        "inventories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_inventories_workspace_name"),
    )
    op.create_index("ix_inventories_workspace_id", "inventories", ["workspace_id"])

    op.create_table(
        "inventory_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inventory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "config", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["inventory_id"], ["inventories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("inventory_id", "position", name="uq_inventory_sources_position"),
    )
    op.create_index("ix_inventory_sources_inventory_id", "inventory_sources", ["inventory_id"])

    op.create_table(
        "inventory_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "groups", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"
        ),
        sa.Column(
            "vars", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_inventory_items_workspace_name"),
    )
    op.create_index("ix_inventory_items_workspace_id", "inventory_items", ["workspace_id"])

    op.create_table(
        "inventory_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inventory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "hosts", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "groups", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("host_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("group_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("produced_by", sa.String(length=16), nullable=False),
        sa.Column("produced_by_ref", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["inventory_id"], ["inventories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inventory_versions_inventory_id", "inventory_versions", ["inventory_id"])


def downgrade() -> None:
    op.drop_index("ix_inventory_versions_inventory_id", table_name="inventory_versions")
    op.drop_table("inventory_versions")
    op.drop_index("ix_inventory_items_workspace_id", table_name="inventory_items")
    op.drop_table("inventory_items")
    op.drop_index("ix_inventory_sources_inventory_id", table_name="inventory_sources")
    op.drop_table("inventory_sources")
    op.drop_index("ix_inventories_workspace_id", table_name="inventories")
    op.drop_table("inventories")
