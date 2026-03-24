"""Consolidated initial schema.

Revision ID: 1253815f7106
Revises:
Create Date: 2026-03-06

All tables for Terrapod v0.1.0 in a single migration.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "1253815f7106"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Users & Auth ---

    op.create_table(
        "users",
        sa.Column("email", sa.String(255), primary_key=True),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "roles",
        sa.Column("name", sa.String(63), primary_key=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "allow_labels",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "allow_names",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "deny_labels",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("deny_names", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("workspace_permission", sa.String(20), nullable=False, server_default="read"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "role_assignments",
        sa.Column("provider_name", sa.String(63), primary_key=True),
        sa.Column("email", sa.String(255), primary_key=True),
        sa.Column(
            "role_name",
            sa.String(63),
            sa.ForeignKey("roles.name", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "platform_role_assignments",
        sa.Column("provider_name", sa.String(63), primary_key=True),
        sa.Column("email", sa.String(255), primary_key=True),
        sa.Column("role_name", sa.String(63), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(63), primary_key=True),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
        sa.Column("user_email", sa.String(255), nullable=True),
        sa.Column("token_type", sa.String(20), nullable=False, server_default="user"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"])
    op.create_index("ix_api_tokens_user_email", "api_tokens", ["user_email"])

    # --- Agent Pools & Listeners ---

    op.create_table(
        "agent_pools",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(63), unique=True, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("service_account_name", sa.String(63), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "agent_pool_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pool_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_pools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("created_by", sa.String(255), nullable=False),
    )
    op.create_index("ix_agent_pool_tokens_pool_id", "agent_pool_tokens", ["pool_id"])
    op.create_index("ix_agent_pool_tokens_token_hash", "agent_pool_tokens", ["token_hash"])

    op.create_table(
        "runner_listeners",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pool_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_pools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(63), unique=True, nullable=False),
        sa.Column("certificate_fingerprint", sa.String(64), nullable=True),
        sa.Column("certificate_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "runner_definitions",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_runner_listeners_pool_id", "runner_listeners", ["pool_id"])

    # --- Certificate Authority ---

    op.create_table(
        "certificate_authority",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("ca_cert", sa.Text(), nullable=False),
        sa.Column("ca_key_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # --- VCS Connections ---

    op.create_table(
        "vcs_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False, server_default="github"),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("server_url", sa.String(500), nullable=False, server_default=""),
        sa.Column("token", sa.Text(), nullable=True),
        sa.Column("github_app_id", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "github_installation_id",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("github_account_login", sa.String(255), nullable=False, server_default=""),
        sa.Column("github_account_type", sa.String(20), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "provider", "github_installation_id", name="uq_vcs_connections_install"
        ),
    )

    # --- Workspaces ---

    op.create_table(
        "workspaces",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(90), nullable=False),
        sa.Column("execution_mode", sa.String(20), nullable=False, server_default="local"),
        sa.Column("auto_apply", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("execution_backend", sa.String(20), nullable=False, server_default="tofu"),
        sa.Column("terraform_version", sa.String(20), nullable=False, server_default="1.11"),
        sa.Column("working_directory", sa.String(500), nullable=False, server_default=""),
        sa.Column("locked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("lock_id", sa.String(255), nullable=True),
        sa.Column(
            "agent_pool_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_pools.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resource_cpu", sa.String(20), nullable=False, server_default="1"),
        sa.Column("resource_memory", sa.String(20), nullable=False, server_default="2Gi"),
        # RBAC
        sa.Column("labels", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("owner_email", sa.String(255), nullable=False, server_default=""),
        # VCS
        sa.Column(
            "vcs_connection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("vcs_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("vcs_repo_url", sa.String(500), nullable=False, server_default=""),
        sa.Column("vcs_branch", sa.String(255), nullable=False, server_default=""),
        sa.Column("vcs_working_directory", sa.String(500), nullable=False, server_default=""),
        sa.Column("vcs_last_commit_sha", sa.String(40), nullable=False, server_default=""),
        # Drift detection
        sa.Column(
            "drift_detection_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "drift_detection_interval_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("86400"),
        ),
        sa.Column("drift_last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("drift_status", sa.String(20), nullable=False, server_default=""),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("name", name="uq_workspaces"),
    )

    op.create_table(
        "state_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("serial", sa.Integer(), nullable=False),
        sa.Column("lineage", sa.String(63), nullable=False, server_default=""),
        sa.Column("md5", sa.String(32), nullable=False, server_default=""),
        sa.Column("state_size", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("workspace_id", "serial", name="uq_state_versions"),
    )
    op.create_index("ix_state_versions_workspace_id", "state_versions", ["workspace_id"])

    # --- Registry ---

    op.create_table(
        "registry_modules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("namespace", sa.String(63), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("provider", sa.String(63), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("labels", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("owner_email", sa.String(255), nullable=False, server_default=""),
        # VCS source tracking
        sa.Column("source", sa.String(20), nullable=False, server_default="upload"),
        sa.Column(
            "vcs_connection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("vcs_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("vcs_repo_url", sa.String(500), nullable=False, server_default=""),
        sa.Column("vcs_branch", sa.String(255), nullable=False, server_default=""),
        sa.Column("vcs_tag_pattern", sa.String(255), nullable=False, server_default="v*"),
        sa.Column("vcs_last_tag", sa.String(255), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("namespace", "name", "provider", name="uq_registry_modules"),
    )

    op.create_table(
        "registry_module_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "module_id",
            UUID(as_uuid=True),
            sa.ForeignKey("registry_modules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.String(63), nullable=False),
        sa.Column("upload_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("vcs_commit_sha", sa.String(64), nullable=False, server_default=""),
        sa.Column("vcs_tag", sa.String(255), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("module_id", "version", name="uq_registry_module_versions"),
    )

    op.create_table(
        "registry_providers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("namespace", sa.String(63), nullable=False),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("labels", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("owner_email", sa.String(255), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("namespace", "name", name="uq_registry_providers"),
    )

    op.create_table(
        "gpg_keys",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("key_id", sa.String(40), nullable=False),
        sa.Column("ascii_armor", sa.Text(), nullable=False),
        sa.Column("source", sa.String(63), nullable=False, server_default="terrapod"),
        sa.Column("source_url", sa.String(500), nullable=True),
        sa.Column("private_key", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("key_id", name="uq_gpg_keys"),
    )

    op.create_table(
        "registry_provider_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "provider_id",
            UUID(as_uuid=True),
            sa.ForeignKey("registry_providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.String(63), nullable=False),
        sa.Column(
            "gpg_key_id",
            UUID(as_uuid=True),
            sa.ForeignKey("gpg_keys.id"),
            nullable=True,
        ),
        sa.Column(
            "protocols",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[\"5.0\"]'::jsonb"),
        ),
        sa.Column(
            "shasums_uploaded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "shasums_sig_uploaded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("provider_id", "version", name="uq_registry_provider_versions"),
    )

    op.create_table(
        "registry_provider_platforms",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("registry_provider_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("os", sa.String(20), nullable=False),
        sa.Column("arch", sa.String(20), nullable=False),
        sa.Column("shasum", sa.String(64), nullable=False, server_default=""),
        sa.Column("filename", sa.String(255), nullable=False, server_default=""),
        sa.Column("upload_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("version_id", "os", "arch", name="uq_registry_provider_platforms"),
    )

    # --- Cache ---

    op.create_table(
        "cached_provider_packages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("hostname", sa.String(255), nullable=False),
        sa.Column("namespace", sa.String(63), nullable=False),
        sa.Column("type", sa.String(63), nullable=False),
        sa.Column("version", sa.String(63), nullable=False),
        sa.Column("os", sa.String(20), nullable=False),
        sa.Column("arch", sa.String(20), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("shasum", sa.String(64), nullable=False),
        sa.Column("h1_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "cached_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "hostname",
            "namespace",
            "type",
            "version",
            "os",
            "arch",
            name="uq_cached_provider_packages",
        ),
    )
    op.create_index(
        "ix_cached_provider_packages_lookup",
        "cached_provider_packages",
        ["hostname", "namespace", "type"],
    )

    op.create_table(
        "cached_binaries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tool", sa.String(20), nullable=False),
        sa.Column("version", sa.String(63), nullable=False),
        sa.Column("os", sa.String(20), nullable=False),
        sa.Column("arch", sa.String(20), nullable=False),
        sa.Column("shasum", sa.String(64), nullable=False, server_default=""),
        sa.Column("download_url", sa.String(1000), nullable=False, server_default=""),
        sa.Column(
            "cached_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tool", "version", "os", "arch", name="uq_cached_binaries"),
    )

    # --- Variables ---

    op.create_table(
        "variables",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("category", sa.String(20), nullable=False, server_default="terraform"),
        sa.Column("hcl", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("version_id", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("workspace_id", "key", name="uq_variables_workspace_key"),
    )
    op.create_index("ix_variables_workspace_id", "variables", ["workspace_id"])

    op.create_table(
        "variable_sets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("global_set", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("priority", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("name", name="uq_variable_sets"),
    )

    op.create_table(
        "variable_set_variables",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "variable_set_id",
            UUID(as_uuid=True),
            sa.ForeignKey("variable_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("category", sa.String(20), nullable=False, server_default="terraform"),
        sa.Column("hcl", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("version_id", sa.String(64), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("variable_set_id", "key", name="uq_variable_set_variables"),
    )
    op.create_index(
        "ix_variable_set_variables_set_id",
        "variable_set_variables",
        ["variable_set_id"],
    )

    op.create_table(
        "variable_set_workspaces",
        sa.Column(
            "variable_set_id",
            UUID(as_uuid=True),
            sa.ForeignKey("variable_sets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )

    # --- Configuration Versions ---

    op.create_table(
        "configuration_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(20), nullable=False, server_default="tfe-api"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "auto_queue_runs",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("speculative", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_configuration_versions_workspace_id",
        "configuration_versions",
        ["workspace_id"],
    )

    # --- Runs ---

    op.create_table(
        "runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "configuration_version_id",
            UUID(as_uuid=True),
            sa.ForeignKey("configuration_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_destroy", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("auto_apply", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("plan_only", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("source", sa.String(30), nullable=False, server_default="tfe-api"),
        sa.Column("execution_backend", sa.String(20), nullable=False, server_default="tofu"),
        sa.Column("terraform_version", sa.String(20), nullable=False, server_default=""),
        sa.Column("resource_cpu", sa.String(20), nullable=False, server_default="1"),
        sa.Column("resource_memory", sa.String(20), nullable=False, server_default="2Gi"),
        sa.Column(
            "pool_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_pools.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "listener_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runner_listeners.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        # Drift detection
        sa.Column(
            "is_drift_detection",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("has_changes", sa.Boolean(), nullable=True),
        # VCS metadata
        sa.Column("vcs_commit_sha", sa.String(40), nullable=False, server_default=""),
        sa.Column("vcs_branch", sa.String(255), nullable=False, server_default=""),
        sa.Column("vcs_pull_request_number", sa.Integer(), nullable=True),
        # Timestamps
        sa.Column("plan_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plan_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("apply_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("apply_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_runs_workspace_id", "runs", ["workspace_id"])
    op.create_index("ix_runs_status", "runs", ["status"])

    # --- Audit Logs ---

    op.create_table(
        "audit_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("actor_email", sa.String(255), nullable=False, server_default=""),
        sa.Column("actor_ip", sa.String(45), nullable=False, server_default=""),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("resource_type", sa.String(63), nullable=False, server_default=""),
        sa.Column("resource_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(63), nullable=False, server_default=""),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_audit_logs_timestamp", "audit_logs", ["timestamp"])
    op.create_index("ix_audit_logs_actor_email", "audit_logs", ["actor_email"])
    op.create_index("ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id"])

    # --- Run Triggers ---

    op.create_table(
        "run_triggers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("workspace_id", "source_workspace_id", name="uq_run_triggers"),
    )
    op.create_index("ix_run_triggers_source_workspace_id", "run_triggers", ["source_workspace_id"])

    # --- Notification Configurations ---

    op.create_table(
        "notification_configurations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("destination_type", sa.String(20), nullable=False),
        sa.Column("url", sa.String(2000), nullable=False, server_default=""),
        sa.Column("token", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("triggers", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "email_addresses",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "delivery_responses",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_notification_configurations_workspace_id",
        "notification_configurations",
        ["workspace_id"],
    )

    # --- Run Tasks ---

    op.create_table(
        "run_tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.String(2000), nullable=False),
        sa.Column("hmac_key", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column(
            "enforcement_level",
            sa.String(20),
            nullable=False,
            server_default="mandatory",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_run_tasks_workspace_id", "run_tasks", ["workspace_id"])

    op.create_table(
        "task_stages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_task_stages_run_id", "task_stages", ["run_id"])

    op.create_table(
        "task_stage_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_stage_id",
            UUID(as_uuid=True),
            sa.ForeignKey("task_stages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("run_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("callback_token", sa.String(255), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_task_stage_results_task_stage_id", "task_stage_results", ["task_stage_id"])


def downgrade() -> None:
    op.drop_table("task_stage_results")
    op.drop_table("task_stages")
    op.drop_table("run_tasks")
    op.drop_table("notification_configurations")
    op.drop_table("run_triggers")
    op.drop_table("audit_logs")
    op.drop_table("runs")
    op.drop_table("configuration_versions")
    op.drop_table("variable_set_workspaces")
    op.drop_table("variable_set_variables")
    op.drop_table("variable_sets")
    op.drop_table("variables")
    op.drop_table("cached_binaries")
    op.drop_table("cached_provider_packages")
    op.drop_table("registry_provider_platforms")
    op.drop_table("registry_provider_versions")
    op.drop_table("gpg_keys")
    op.drop_table("registry_providers")
    op.drop_table("registry_module_versions")
    op.drop_table("registry_modules")
    op.drop_table("state_versions")
    op.drop_table("workspaces")
    op.drop_table("vcs_connections")
    op.drop_table("certificate_authority")
    op.drop_table("runner_listeners")
    op.drop_table("agent_pool_tokens")
    op.drop_table("agent_pools")
    op.drop_table("api_tokens")
    op.drop_table("platform_role_assignments")
    op.drop_table("role_assignments")
    op.drop_table("roles")
    op.drop_table("users")
