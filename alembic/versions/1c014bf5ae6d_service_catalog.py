"""Service Catalog (#535): catalog_items + provider_templates, workspace
provenance columns, and the role catalog_permission axis.

A catalog item is a blessed, version-pinnable designation over a registry
module; a provider template is an admin-managed parameterized provider config
(rendered into the generated wrapper's ROOT). Provisioning creates an ordinary
agent-mode, non-VCS workspace whose provenance columns mark it catalog-managed.

Revision ID: 1c014bf5ae6d
Revises: ea933f083c6b
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "1c014bf5ae6d"
down_revision = "ea933f083c6b"


def upgrade() -> None:
    op.create_table(
        "provider_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("provider_type", sa.String(63), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("labels", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("owner_email", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_provider_templates"),
    )

    op.create_table(
        "catalog_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "module_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("registry_modules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("default_version_pin", sa.String(63), nullable=True),
        sa.Column("name", sa.String(90), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "provider_template_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("allowed_agent_pool_ids", postgresql.JSONB(), nullable=True),
        sa.Column("variable_options", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("labels", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("owner_email", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", name="uq_catalog_items"),
    )
    op.create_index("ix_catalog_items_module_id", "catalog_items", ["module_id"])

    # Role catalog-access axis. Opt-in: existing roles get "none".
    op.add_column(
        "roles",
        sa.Column(
            "catalog_permission",
            sa.String(20),
            nullable=False,
            server_default="none",
        ),
    )

    # Workspace provenance (catalog instance). All nullable — existing
    # workspaces are not catalog-managed.
    op.add_column(
        "workspaces",
        sa.Column(
            "catalog_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("catalog_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("workspaces", sa.Column("catalog_version_pin", sa.String(63), nullable=True))
    op.add_column(
        "workspaces",
        sa.Column("catalog_input_values", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "catalog_input_values")
    op.drop_column("workspaces", "catalog_version_pin")
    op.drop_column("workspaces", "catalog_item_id")
    op.drop_column("roles", "catalog_permission")
    op.drop_index("ix_catalog_items_module_id", table_name="catalog_items")
    op.drop_table("catalog_items")
    op.drop_table("provider_templates")
