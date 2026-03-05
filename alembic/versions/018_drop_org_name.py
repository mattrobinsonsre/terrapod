"""Drop org_name columns from all tables.

Terrapod is single-organization by design. The "default" org exists solely
as a static string in TFE-compatible API paths. There is no multi-org
support at any level. This migration removes the vestigial org_name columns
and updates unique constraints accordingly.

Revision ID: 018
Revises: 017
"""

from alembic import op
import sqlalchemy as sa

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- workspaces ---
    op.drop_constraint("uq_workspaces", "workspaces", type_="unique")
    op.create_unique_constraint("uq_workspaces", "workspaces", ["name"])
    op.drop_column("workspaces", "org_name")

    # --- registry_modules ---
    op.drop_constraint("uq_registry_modules", "registry_modules", type_="unique")
    op.create_unique_constraint(
        "uq_registry_modules", "registry_modules", ["namespace", "name", "provider"]
    )
    op.drop_column("registry_modules", "org_name")

    # --- registry_providers ---
    op.drop_constraint("uq_registry_providers", "registry_providers", type_="unique")
    op.create_unique_constraint(
        "uq_registry_providers", "registry_providers", ["namespace", "name"]
    )
    op.drop_column("registry_providers", "org_name")

    # --- gpg_keys ---
    op.drop_constraint("uq_gpg_keys", "gpg_keys", type_="unique")
    op.create_unique_constraint("uq_gpg_keys", "gpg_keys", ["key_id"])
    op.drop_column("gpg_keys", "org_name")

    # --- variable_sets ---
    op.drop_constraint("uq_variable_sets", "variable_sets", type_="unique")
    op.create_unique_constraint("uq_variable_sets", "variable_sets", ["name"])
    op.drop_column("variable_sets", "org_name")

    # --- vcs_connections (no unique constraint on org_name, just drop column) ---
    op.drop_column("vcs_connections", "org_name")

    # --- agent_pools (nullable, no unique constraint on org_name) ---
    op.drop_column("agent_pools", "org_name")

    # --- api_tokens (nullable, no unique constraint on org_name) ---
    op.drop_column("api_tokens", "org_name")


def downgrade() -> None:
    # Re-add all org_name columns with default "default"
    op.add_column(
        "api_tokens",
        sa.Column("org_name", sa.String(63), nullable=True),
    )
    op.add_column(
        "agent_pools",
        sa.Column("org_name", sa.String(63), nullable=True),
    )
    op.add_column(
        "vcs_connections",
        sa.Column("org_name", sa.String(63), nullable=False, server_default="default"),
    )

    # variable_sets
    op.add_column(
        "variable_sets",
        sa.Column("org_name", sa.String(63), nullable=False, server_default="default"),
    )
    op.drop_constraint("uq_variable_sets", "variable_sets", type_="unique")
    op.create_unique_constraint("uq_variable_sets", "variable_sets", ["org_name", "name"])

    # gpg_keys
    op.add_column(
        "gpg_keys",
        sa.Column("org_name", sa.String(63), nullable=False, server_default="default"),
    )
    op.drop_constraint("uq_gpg_keys", "gpg_keys", type_="unique")
    op.create_unique_constraint("uq_gpg_keys", "gpg_keys", ["org_name", "key_id"])

    # registry_providers
    op.add_column(
        "registry_providers",
        sa.Column("org_name", sa.String(63), nullable=False, server_default="default"),
    )
    op.drop_constraint("uq_registry_providers", "registry_providers", type_="unique")
    op.create_unique_constraint(
        "uq_registry_providers", "registry_providers", ["org_name", "namespace", "name"]
    )

    # registry_modules
    op.add_column(
        "registry_modules",
        sa.Column("org_name", sa.String(63), nullable=False, server_default="default"),
    )
    op.drop_constraint("uq_registry_modules", "registry_modules", type_="unique")
    op.create_unique_constraint(
        "uq_registry_modules", "registry_modules", ["org_name", "namespace", "name", "provider"]
    )

    # workspaces
    op.add_column(
        "workspaces",
        sa.Column("org_name", sa.String(63), nullable=False, server_default="default"),
    )
    op.drop_constraint("uq_workspaces", "workspaces", type_="unique")
    op.create_unique_constraint("uq_workspaces", "workspaces", ["org_name", "name"])
