"""
SQLAlchemy database models for Terrapod.

All models use:
- UUIDv7 primary keys (time-sortable), except users (email PK)
- snake_case column names
- Plural table names
- TIMESTAMPTZ with UTC for all timestamps
- Hard deletes (no soft delete columns)
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def generate_uuid7() -> uuid.UUID:
    """Generate a UUIDv7 (time-sortable UUID)."""
    import time

    timestamp_ms = int(time.time() * 1000)
    rand_bytes = uuid.uuid4().bytes[6:]

    uuid_bytes = (
        timestamp_ms.to_bytes(6, "big")
        + bytes([0x70 | (rand_bytes[0] & 0x0F)])  # Version 7
        + bytes([0x80 | (rand_bytes[1] & 0x3F)])  # Variant
        + rand_bytes[2:]
    )
    return uuid.UUID(bytes=uuid_bytes)


def now_utc() -> datetime:
    """Get current UTC datetime."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Base class for all models."""

    type_annotation_map = {
        dict[str, Any]: JSONB,
    }


class User(Base):
    """User account model.

    PK is email (natural key). Users are identified by email across all
    authentication providers. Permissions live on roles, not on individual users.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )


class Role(Base):
    """Custom role model for RBAC.

    Only admin-created roles live here. Built-in roles (admin, audit,
    everyone) are defined in terrapod.auth.builtin_roles — not in the database.
    """

    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(63), primary_key=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Permissions
    allow_labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    allow_names: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    deny_labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    deny_names: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    workspace_permission: Mapped[str] = mapped_column(
        String(20), nullable=False, default="read"
    )  # read, plan, write, admin
    pool_permission: Mapped[str] = mapped_column(
        String(20), nullable=False, default="read", server_default="read"
    )  # read, write, admin
    registry_permission: Mapped[str] = mapped_column(
        String(20), nullable=False, default="read", server_default="read"
    )  # read, write, admin — modules + providers share this
    # Service Catalog access (#535). Opt-in (default none): read = browse
    # label-scoped items; use = provision + manage own instances via the
    # catalog API only; admin = author items/provider-templates + manage all.
    catalog_permission: Mapped[str] = mapped_column(
        String(20), nullable=False, default="none", server_default="none"
    )  # none, read, use, admin

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )


class RoleAssignment(Base):
    """Maps (provider, email) pairs to custom role names.

    FK to roles.name ensures only valid custom roles can be assigned.
    Platform roles (admin, audit) go in platform_role_assignments instead.
    """

    __tablename__ = "role_assignments"

    provider_name: Mapped[str] = mapped_column(String(63), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    role_name: Mapped[str] = mapped_column(
        String(63), ForeignKey("roles.name", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )


class PlatformRoleAssignment(Base):
    """Admin and audit role assignments.

    Separate from role_assignments (which has FK to custom roles) because
    admin and audit are built-in platform roles with no row in the roles table.
    """

    __tablename__ = "platform_role_assignments"

    provider_name: Mapped[str] = mapped_column(String(63), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    role_name: Mapped[str] = mapped_column(String(63), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )


class APIToken(Base):
    """Long-lived API tokens for terraform CLI and automation.

    Tokens are hashed at rest (SHA-256). The raw token value is only
    returned once at creation time. Lookup by hash on every request
    (indexed column).
    """

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(63), primary_key=True)  # "at-{uuid7}"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Token kind (#495): interactive (terraform login / UI), service_bound
    # (user-bound automation; perms = min(pinned, owner live)), service_detached
    # (admin-managed M2M; absolute pinned perms; exempt from idle-login rejection).
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="interactive")
    # Owning identity. NULL <=> detached (no user binding). Bare email string,
    # NOT an FK: SSO users have no `users` row (sso_service: "SSO users are NOT
    # stored in the users table"), so an FK would reject their tokens. Integrity
    # via live role-intersection + idle-login window + local is_active check.
    bound_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The minter (audit). Always set, even for detached (its only creator record).
    created_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Token's own pinned role set (service tokens). Resolved through label-RBAC.
    pinned_roles: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # Legacy TFE-shaped field, superseded by `kind`. Retained (unread) for
    # response back-compat; dropped in a later release.
    token_type: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    # Set by the rotate action; expiry basis = rotated_at or created_at.
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifespan_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (Index("ix_api_tokens_bound_to", "bound_to"),)


class AgentPool(Base):
    """Named group of runner listeners.

    Workspaces are assigned to an agent pool for execution.
    Pools are created by admins via the API. Listeners join pools
    using join tokens and receive X.509 certificates for auth.
    """

    __tablename__ = "agent_pools"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    name: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # RBAC
    labels: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    owner_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    tokens: Mapped[list["AgentPoolToken"]] = relationship(
        back_populates="pool", passive_deletes=True
    )


class AgentPoolToken(Base):
    """Join token for listener registration.

    SHA-256 hashed at rest. The raw token is only returned once at creation.
    Supports expiry and optional max_uses.
    """

    __tablename__ = "agent_pool_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_pools.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    pool: Mapped["AgentPool"] = relationship(back_populates="tokens")

    __table_args__ = (Index("ix_agent_pool_tokens_pool_id", "pool_id"),)


# --- Workspace Models ---


class Workspace(Base):
    """Terraform workspace — isolates state, variables, and runs."""

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    name: Mapped[str] = mapped_column(String(90), nullable=False)
    execution_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="local"
    )  # local, agent
    auto_apply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    execution_backend: Mapped[str] = mapped_column(
        String(20), nullable=False, default="tofu"
    )  # tofu, terraform
    terraform_version: Mapped[str] = mapped_column(String(20), nullable=False, default="1.12")
    # Terragrunt single-unit support (#534): when enabled the runner invokes
    # `terragrunt` wrapping the tofu/terraform binary (via TG_TF_PATH). Version
    # is partial (e.g. "0.67"), resolved via the binary cache like
    # terraform_version; empty → latest stable.
    terragrunt_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    terragrunt_version: Mapped[str] = mapped_column(String(20), nullable=False, default="1.0")
    working_directory: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    lock_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_pools.id", ondelete="SET NULL"),
        nullable=True,
    )
    agent_pool: Mapped["AgentPool | None"] = relationship(
        "AgentPool", foreign_keys=[agent_pool_id], lazy="joined"
    )
    resource_cpu: Mapped[str] = mapped_column(String(20), nullable=False, default="1")
    resource_memory: Mapped[str] = mapped_column(String(20), nullable=False, default="2Gi")

    # RBAC
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    owner_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # VCS integration
    vcs_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vcs_connections.id", ondelete="SET NULL"),
        nullable=True,
    )
    vcs_connection: Mapped["VCSConnection | None"] = relationship(
        "VCSConnection", foreign_keys=[vcs_connection_id], lazy="joined"
    )
    vcs_repo_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    vcs_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    vcs_last_commit_sha: Mapped[str] = mapped_column(String(40), nullable=False, default="")

    # VCS polling health
    vcs_last_polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    vcs_last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    vcs_last_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # tfvars files — passed to runner as -var-file arguments
    var_files: Mapped[list[str]] = mapped_column(
        ARRAY(String(500)), nullable=False, server_default="{}"
    )

    # VCS trigger prefixes — directories that trigger runs (overrides working_directory filtering)
    trigger_prefixes: Mapped[list[str]] = mapped_column(
        ARRAY(String(255)), nullable=False, server_default="{}"
    )

    # VCS workflow mode — see #282 + docs/vcs-workflows.md
    # - merge_then_apply (default, TFE/HCP standard): PR runs are speculative;
    #   apply happens against the merged commit on the default branch.
    # - apply_then_merge (Atlantis standard, opt-in): PR runs are full
    #   plan-and-apply with saved tfplan; apply runs against the PR head and
    #   the user drives apply via PR comments.
    vcs_workflow: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="merge_then_apply", default="merge_then_apply"
    )

    # Auto-merge after apply succeeds. Available in both modes; primary use is
    # apply_then_merge. When all PR-affected workspaces meet their per-mode
    # required state, the merge fires via the VCS provider's merge API.
    auto_merge: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    auto_merge_strategy: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="merge", default="merge"
    )  # merge, squash, rebase

    # Drift detection
    drift_detection_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    drift_detection_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=86400
    )
    drift_last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    drift_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=""
    )  # "", "no_drift", "drifted", "errored"
    # The run that produced the current `drift_status`. Lets the workspace-list
    # UI link the Drifted / Errored badge straight to the run, instead of the
    # operator hunting through the runs list. Plain nullable UUID, no FK — a
    # later artifact-retention sweep may delete the run row, and we don't want
    # that to break workspace deletion via cascade. The UI handles a 404 on the
    # click-through gracefully.
    drift_latest_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Per-workspace allowlist of resource-address + attribute-path glob
    # patterns (#482). When a drift run reports `has_changes=True`, every
    # changed attribute is matched against this list before drift_status
    # is computed. If every change matches a rule → drift_status=no_drift.
    # Empty list (default) means classic behaviour: every change counts
    # as drift. Examples:
    #   "aws_iam_role.foo.tags.Environment"   (single attr on one resource)
    #   "module.eks*.argocd_cluster.*.config.tls_client_config.ca_data"
    #   "aws_autoscaling_group.workers[*]"   (any change to any workers[i])
    drift_ignore_rules: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]", default=list
    )

    # State divergence — set when an apply Job succeeds but state upload fails
    state_diverged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # AI plan summary (#401). `ai_summary_mode` is a three-state opt-in:
    # - "default": follow the global ai_summary.enabled toggle
    # - "enabled": always summarise (no-op if global is disabled — the UI
    #   does not offer this state when global is off)
    # - "disabled": never summarise, even when global is enabled
    # `ai_summary_context` is workspace-specific facts added as the last
    # layer of context before the plan JSON; it is ADDITIVE to the global
    # fleet_context rather than replacing it.
    ai_summary_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="default", default="default"
    )
    ai_summary_context: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="", default=""
    )

    # Autodiscovery lifecycle (#314). lifecycle_state: active (normal) |
    # pending_deletion (origin dir/PR gone — needs explicit operator
    # action; NEVER auto-destroyed) | archived (soft-deleted after a
    # successful opt-in destroy, or a never-applied orphan auto-cleaned).
    lifecycle_state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="active", default="active"
    )
    lifecycle_reason: Mapped[str] = mapped_column(
        String(500), nullable=False, server_default="", default=""
    )
    # The PR that materialised this autodiscovered workspace (NULL for
    # non-autodiscovered or initial-scan-created). Lets the poller
    # reconcile when that PR is closed-unmerged / no longer matches.
    autodiscovery_pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Autodiscovery — set when this workspace was auto-created by an
    # AutodiscoveryRule rather than manually. Used to attribute
    # workspaces in the audit log and to skip re-creation on subsequent
    # poll cycles.
    autodiscovery_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("autodiscovery_rules.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Service Catalog provenance (#535). Set when this workspace was
    # provisioned from a catalog item — the workspace IS the instance. A
    # workspace is "catalog-managed" iff catalog_item_id is not NULL: such
    # workspaces are admin-managed (non-admins act on them only through the
    # catalog API). catalog_version_pin NULL = float (track latest module
    # version via the module_workspace_link); set = pinned. catalog_input_values
    # is the (sensitive-redacted) snapshot of the provision form values.
    catalog_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("catalog_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    catalog_version_pin: Mapped[str | None] = mapped_column(String(63), nullable=True)
    catalog_input_values: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    state_versions: Mapped[list["StateVersion"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    variables: Mapped[list["Variable"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    runs: Mapped[list["Run"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )

    __table_args__ = (sa.UniqueConstraint("name", name="uq_workspaces"),)


class StateVersion(Base):
    """Versioned Terraform state for a workspace.

    State data (JSON) is stored in object storage at
    state/{workspace_id}/{serial}.json. This table tracks metadata.
    """

    __tablename__ = "state_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    serial: Mapped[int] = mapped_column(Integer, nullable=False)
    lineage: Mapped[str] = mapped_column(String(63), nullable=False, default="")
    # `md5` is part of the TFE/go-tfe state-version contract and is kept for
    # compatibility. `sha256` is Terrapod-internal and is the hash used for
    # the state-divergence equality check (an md5 collision must not be able
    # to suppress a genuine divergence flag). Nullable/empty for rows written
    # before the column existed — the divergence check falls back to md5 then.
    md5: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    state_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="state_versions")

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "serial", name="uq_state_versions"),
        Index("ix_state_versions_workspace_id", "workspace_id"),
        Index("ix_state_versions_run_id", "run_id"),
    )


# --- Registry Models ---


class RegistryModule(Base):
    """Top-level module entity in the private registry.

    Identified by (namespace, name, provider).
    """

    __tablename__ = "registry_modules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    namespace: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    provider: Mapped[str] = mapped_column(String(63), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    owner_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # VCS source tracking
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="upload")
    vcs_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vcs_connections.id", ondelete="SET NULL"),
        nullable=True,
    )
    vcs_repo_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    vcs_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    vcs_tag_pattern: Mapped[str] = mapped_column(String(255), nullable=False, default="v*")
    vcs_last_tag: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    vcs_last_pr_shas: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    versions: Mapped[list["RegistryModuleVersion"]] = relationship(
        back_populates="module", cascade="all, delete-orphan"
    )
    workspace_links: Mapped[list["ModuleWorkspaceLink"]] = relationship(
        back_populates="module", cascade="all, delete-orphan"
    )

    __table_args__ = (
        sa.UniqueConstraint("namespace", "name", "provider", name="uq_registry_modules"),
    )


class RegistryModuleVersion(Base):
    """Semver version of a registry module, backed by a tarball in object storage."""

    __tablename__ = "registry_module_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("registry_modules.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(String(63), nullable=False)
    upload_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    vcs_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    vcs_tag: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    inputs: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    outputs: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    module: Mapped["RegistryModule"] = relationship(back_populates="versions")

    __table_args__ = (
        sa.UniqueConstraint("module_id", "version", name="uq_registry_module_versions"),
    )


class ModuleWorkspaceLink(Base):
    """Links a registry module to a consuming workspace.

    When a PR is opened against the module's VCS repo, speculative plan-only
    runs are automatically queued on linked workspaces. When a new module
    version is published, standard runs are queued on linked workspaces.
    """

    __tablename__ = "module_workspace_links"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("registry_modules.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)

    module: Mapped["RegistryModule"] = relationship(back_populates="workspace_links")
    workspace: Mapped["Workspace"] = relationship(lazy="joined")

    __table_args__ = (
        sa.UniqueConstraint("module_id", "workspace_id", name="uq_module_workspace_links"),
        Index("ix_module_workspace_links_module_id", "module_id"),
        Index("ix_module_workspace_links_workspace_id", "workspace_id"),
    )


class ProviderTemplate(Base):
    """Admin-managed, parameterized provider configuration for the Service
    Catalog (#535).

    Managed like a policy set (CRUD + RBAC + audit), authored as code via the
    Terraform provider. `body` is an HCL `provider "x" { ... }` snippet that
    references `var.*`; `parameters` declares the inputs (region, account_id,
    …) the consumer fills at provision. The catalog wrapper renders the body
    into the generated ROOT module (never into the registry module) and
    declares the params as root variables — values flow as Terraform variables,
    so there is no server-side string interpolation.
    """

    __tablename__ = "provider_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(63), nullable=False)  # aws, gcp, azure, …
    body: Mapped[str] = mapped_column(Text, nullable=False)  # HCL provider block referencing var.*
    # [{name, type, required, default, sensitive}] — the consumer-supplied params.
    parameters: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    owner_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    __table_args__ = (sa.UniqueConstraint("name", name="uq_provider_templates"),)


class CatalogItem(Base):
    """A blessed, version-pinnable designation over a registry module for
    no-code self-service provisioning (#535).

    NOT a copy of the module — a designation + form-constraint overlay. The
    provisioning form derives from the module version's extracted interface
    (RegistryModuleVersion.inputs) plus `variable_options`; provisioning
    creates an ordinary agent-mode, non-VCS workspace whose config is a
    server-generated wrapper that calls this module.
    """

    __tablename__ = "catalog_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("registry_modules.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Item-level default pin (NULL = float / track latest; instances may override).
    default_version_pin: Mapped[str | None] = mapped_column(String(63), nullable=True)
    name: Mapped[str] = mapped_column(String(90), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # FK lists kept as JSONB (not association tables) — always loaded with the item.
    provider_template_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    # NULL/empty = any pool the consumer can use; else scopes the offered pools.
    allowed_agent_pool_ids: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    # Per-variable overlay: [{name, options[], default, sensitive, hidden}].
    # `hidden: true` (with a fixed `default`) removes the input from the
    # provision form and wires the default; `options` constrains the choice;
    # `default` presets an editable value.
    variable_options: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    owner_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    module: Mapped["RegistryModule"] = relationship(lazy="joined")

    __table_args__ = (
        sa.UniqueConstraint("name", name="uq_catalog_items"),
        Index("ix_catalog_items_module_id", "module_id"),
    )


class RegistryProvider(Base):
    """Top-level provider entity in the private registry."""

    __tablename__ = "registry_providers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    namespace: Mapped[str] = mapped_column(String(63), nullable=False)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    owner_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    versions: Mapped[list["RegistryProviderVersion"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan"
    )

    __table_args__ = (sa.UniqueConstraint("namespace", "name", name="uq_registry_providers"),)


class GPGKey(Base):
    """GPG public key for provider signing verification."""

    __tablename__ = "gpg_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    key_id: Mapped[str] = mapped_column(String(40), nullable=False)
    ascii_armor: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(63), nullable=False, default="terrapod")
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    private_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    __table_args__ = (sa.UniqueConstraint("key_id", name="uq_gpg_keys"),)


class RegistryProviderVersion(Base):
    """Version of a registry provider with GPG key ref and shasums tracking."""

    __tablename__ = "registry_provider_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("registry_providers.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(String(63), nullable=False)
    gpg_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("gpg_keys.id"),
        nullable=True,
    )
    protocols: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: ["5.0"]
    )
    shasums_uploaded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    shasums_sig_uploaded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    provider: Mapped["RegistryProvider"] = relationship(back_populates="versions")
    platforms: Mapped[list["RegistryProviderPlatform"]] = relationship(
        back_populates="version", cascade="all, delete-orphan"
    )
    gpg_key: Mapped["GPGKey | None"] = relationship()

    __table_args__ = (
        sa.UniqueConstraint("provider_id", "version", name="uq_registry_provider_versions"),
    )


class RegistryProviderPlatform(Base):
    """Per-OS/arch binary for a provider version."""

    __tablename__ = "registry_provider_platforms"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("registry_provider_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    os: Mapped[str] = mapped_column(String(20), nullable=False)
    arch: Mapped[str] = mapped_column(String(20), nullable=False)
    shasum: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    upload_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    h1_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    version: Mapped["RegistryProviderVersion"] = relationship(back_populates="platforms")

    __table_args__ = (
        sa.UniqueConstraint("version_id", "os", "arch", name="uq_registry_provider_platforms"),
    )


# --- Cache Models ---


class CachedProviderPackage(Base):
    """Cached upstream provider binary from a public registry."""

    __tablename__ = "cached_provider_packages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    namespace: Mapped[str] = mapped_column(String(63), nullable=False)
    type: Mapped[str] = mapped_column(String(63), nullable=False)
    version: Mapped[str] = mapped_column(String(63), nullable=False)
    os: Mapped[str] = mapped_column(String(20), nullable=False)
    arch: Mapped[str] = mapped_column(String(20), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    shasum: Mapped[str] = mapped_column(String(64), nullable=False)
    h1_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    cached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "hostname",
            "namespace",
            "type",
            "version",
            "os",
            "arch",
            name="uq_cached_provider_packages",
        ),
        Index("ix_cached_provider_packages_lookup", "hostname", "namespace", "type"),
    )


class CachedBinary(Base):
    """Cached terraform/tofu CLI binary."""

    __tablename__ = "cached_binaries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    tool: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[str] = mapped_column(String(63), nullable=False)
    os: Mapped[str] = mapped_column(String(20), nullable=False)
    arch: Mapped[str] = mapped_column(String(20), nullable=False)
    shasum: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    download_url: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    cached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint("tool", "version", "os", "arch", name="uq_cached_binaries"),
    )


# --- Certificate Authority ---


class CertificateAuthorityModel(Base):
    """CA certificate and key, persisted for cross-restart identity.

    Single row table. Created on first startup by init_ca().
    """

    __tablename__ = "certificate_authority"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    ca_cert: Mapped[str] = mapped_column(Text, nullable=False)
    # The CA private key as a PKCS8 PEM. This is NOT application-encrypted
    # (the old column name `ca_key_encrypted` was misleading) — at-rest
    # protection comes from database encryption (RDS/Azure/GCS-managed),
    # the same model used for sensitive variables and VCS tokens.
    ca_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )


# --- VCS Connections ---


class VCSConnection(Base):
    """VCS provider connection.

    Supports GitHub (App installation), GitLab (project/group access token),
    and generic git hosts. Provider-specific fields are nullable and only
    populated for their respective provider type.
    """

    __tablename__ = "vcs_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="github"
    )  # github, gitlab
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Provider-agnostic fields
    server_url: Mapped[str] = mapped_column(
        String(500), nullable=False, default=""
    )  # e.g. https://gitlab.example.com, https://github.example.com
    token: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # PAT (GitLab) or PEM private key (GitHub App)

    # GitHub-specific
    github_app_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    github_installation_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    github_account_login: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    github_account_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=""
    )  # Organization, User (GitHub only)

    # Per-connection webhook HMAC secret (GitHub). Optional: when set it is
    # used to validate this connection's inbound webhooks, falling back to the
    # global vcs.github.webhook_secret when null. Write-only via the API and
    # protected at rest by database encryption (same as `token`).
    webhook_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )  # active, suspended, removed

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "provider", "github_installation_id", name="uq_vcs_connections_install"
        ),
    )


# --- Autodiscovery (Atlantis-style) ---


class AutodiscoveryRule(Base):
    """Connection-scoped rule that auto-creates a workspace when a PR
    or default-branch push touches a path matching `pattern`.

    Modelled on Atlantis's `repos.yaml` autodiscover block. Workspaces
    are created with the rule's template fields (execution mode, agent
    pool, terraform version, resources, labels, owner) inherited.
    """

    __tablename__ = "autodiscovery_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )

    # Scoping
    vcs_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vcs_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    vcs_connection: Mapped["VCSConnection"] = relationship(
        "VCSConnection", foreign_keys=[vcs_connection_id], lazy="joined"
    )
    repo_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # Match
    pattern: Mapped[str] = mapped_column(String(1024), nullable=False)
    ignore_patterns: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    # Identity
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_template: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Workspace template — defaults inherited by auto-created workspaces.
    execution_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="agent")
    agent_pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_pools.id", ondelete="SET NULL"),
        nullable=True,
    )
    execution_backend: Mapped[str] = mapped_column(String(20), nullable=False, default="tofu")
    terraform_version: Mapped[str] = mapped_column(String(50), nullable=False, default="1.12")
    resource_cpu: Mapped[str] = mapped_column(String(20), nullable=False, default="1")
    resource_memory: Mapped[str] = mapped_column(String(20), nullable=False, default="2Gi")
    auto_apply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    owner_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # #318: settings provisioned on every workspace this rule materialises,
    # so autodiscovered workspaces are fully configured at creation.
    var_files: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    run_task_templates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    notification_templates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    # #314 deletion lifecycle: what to do when a discovered directory is
    # removed on the tracked branch. "flag" (default, safe) marks the
    # workspace pending_deletion and requires an explicit operator
    # action; "destroy" (opt-in, for ephemeral envs) queues a real
    # destroy run then archives. NEVER silently destroys.
    on_directory_delete: Mapped[str] = mapped_column(
        String(10), nullable=False, default="flag", server_default="flag"
    )

    # Set on first successful full-tree scan of the repo. NULL means the
    # rule has never been backfilled — the poll cycle picks rules where
    # this is NULL and walks the full repo tree once (in addition to its
    # normal changed-files walk) so existing matching directories get
    # workspaces without waiting for someone to touch each one. Cleared
    # to NULL whenever `enabled` flips false → true so a re-enable also
    # re-scans. See #309.
    first_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Audit
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint("vcs_connection_id", "name", name="uq_autodiscovery_rule_name"),
    )


class PRSession(Base):
    """Conversation state for one PR/MR in apply-then-merge mode (#282).

    Tracks the edit-in-place status comment, the current head SHA, the
    poll cursors for comments/reviews, and the lifecycle state of the PR.
    One row per (connection, repo, pr_number). Created lazily when the
    first apply-then-merge workspace plans against the PR.
    """

    __tablename__ = "pr_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )

    vcs_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vcs_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    vcs_connection: Mapped["VCSConnection"] = relationship(
        "VCSConnection", foreign_keys=[vcs_connection_id], lazy="joined"
    )
    repo: Mapped[str] = mapped_column(String(500), nullable=False)  # owner/name
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(40), nullable=False, default="")

    # Edit-in-place status comment id from the VCS provider (string because
    # GitHub uses int64s but GitLab and other providers may differ).
    status_comment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Poll cursors — last item we've processed via webhook or poll, so the
    # poll fallback doesn't re-dispatch already-handled events. Strings to
    # accommodate provider-specific id shapes.
    last_processed_comment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_processed_review_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # PR lifecycle state. Mirrors the VCS provider's notion: open, closed
    # (without merge), or merged. Closed/merged sessions are kept for
    # historical audit but don't dispatch commands.
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="open")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint("vcs_connection_id", "repo", "pr_number", name="uq_pr_session"),
        sa.Index("ix_pr_sessions_open", "vcs_connection_id", "state"),
    )


# --- Variables ---


class Variable(Base):
    """Workspace-scoped variable (terraform or env).

    Sensitive values are stored in the value column with the sensitive flag
    set to True. API responses mask sensitive values.
    """

    __tablename__ = "variables"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(
        String(20), nullable=False, default="terraform"
    )  # "terraform" or "env"
    hcl: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="variables")

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "key", name="uq_variables_workspace_key"),
        Index("ix_variables_workspace_id", "workspace_id"),
    )


class VariableSet(Base):
    """Organization-scoped variable set, applicable to multiple workspaces."""

    __tablename__ = "variable_sets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    global_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    priority: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    variables: Mapped[list["VariableSetVariable"]] = relationship(
        back_populates="variable_set", cascade="all, delete-orphan"
    )
    workspace_assignments: Mapped[list["VariableSetWorkspace"]] = relationship(
        cascade="all, delete-orphan"
    )

    __table_args__ = (sa.UniqueConstraint("name", name="uq_variable_sets"),)


class VariableSetVariable(Base):
    """Variable within a variable set."""

    __tablename__ = "variable_set_variables"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    variable_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("variable_sets.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(20), nullable=False, default="terraform")
    hcl: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    variable_set: Mapped["VariableSet"] = relationship(back_populates="variables")

    __table_args__ = (
        sa.UniqueConstraint("variable_set_id", "key", name="uq_variable_set_variables"),
        Index("ix_variable_set_variables_set_id", "variable_set_id"),
    )


class VariableSetWorkspace(Base):
    """Junction table linking variable sets to workspaces."""

    __tablename__ = "variable_set_workspaces"

    variable_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("variable_sets.id", ondelete="CASCADE"),
        primary_key=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )

    workspace: Mapped["Workspace"] = relationship(lazy="joined")


# --- Configuration Versions ---


class ConfigurationVersion(Base):
    """Configuration version — uploaded HCL source code for a run."""

    __tablename__ = "configuration_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="tfe-api"
    )  # tfe-api, vcs, cli
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending, uploading, uploaded, errored
    auto_queue_runs: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    speculative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (Index("ix_configuration_versions_workspace_id", "workspace_id"),)


# --- Runs ---


class Run(Base):
    """A plan/apply execution against a workspace.

    State machine: pending → queued → planning → planned → confirmed →
    applying → applied. Error/cancel transitions from any non-terminal state.
    """

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    configuration_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("configuration_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_destroy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_apply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    plan_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="tfe-api")
    execution_backend: Mapped[str] = mapped_column(
        String(20), nullable=False, default="tofu"
    )  # tofu, terraform
    terraform_version: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    # Terragrunt snapshot (#534) — frozen from the workspace at run creation so
    # later workspace edits don't change an in-flight run's execution tool.
    terragrunt_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    terragrunt_version: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    resource_cpu: Mapped[str] = mapped_column(String(20), nullable=False, default="1")
    resource_memory: Mapped[str] = mapped_column(String(20), nullable=False, default="2Gi")
    pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_pools.id", ondelete="SET NULL"),
        nullable=True,
    )
    listener_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Runner resource profile + OOM detection (#430).
    # peak_memory_bytes / peak_cpu_usec — captured by the runner at exit
    #   (cgroup v2 /sys/fs/cgroup/memory.peak + cpu.stat usage_usec).
    # runner_exit_code / runner_exit_reason — set by the listener when
    #   the K8s Job terminates (exit code + container.state.terminated.reason).
    # runner_exit_status — Terrapod-side typed bucket distinguishing
    #   "" (unset / pre-feature run), "clean" (runner exited 0),
    #   "oom" (OOMKilled), "killed" (other SIGKILL), "error" (nonzero exit).
    #   Lets the run UX and AI summary gate behave consistently regardless
    #   of which signal (cgroup vs K8s) actually triggered detection.
    peak_memory_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    peak_cpu_usec: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    runner_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runner_exit_reason: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    runner_exit_status: Mapped[str] = mapped_column(String(20), nullable=False, default="")

    # Run options (CLI flags)
    target_addrs: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    replace_addrs: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    refresh_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    refresh: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_empty_apply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Module impact analysis overrides (maps module coords to override storage paths)
    module_overrides: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # Drift detection
    is_drift_detection: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_changes: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # True once the runner has uploaded `tofu show -json` output to storage.
    # Drives the optional `json-output` URL in plan responses so we don't
    # advertise a 404 on errored / older / not-yet-uploaded runs.
    has_json_output: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    # Plan resource-change counts parsed from the JSON plan output on
    # upload. Null until parsed (older runs, or parse failed); zero means
    # parsed-and-no-resources-of-that-kind. The UI distinguishes the two.
    resource_additions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resource_changes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resource_destructions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resource_replacements: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resource_imports: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Job tracking (populated by listener after launching K8s Job)
    job_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_namespace: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # VCS metadata
    vcs_commit_sha: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    vcs_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    vcs_pull_request_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Apply-then-merge bookkeeping (#282)
    # Populated when the apply gate rejects (e.g. branch protection blocks merge);
    # surfaced on the run UI and on the PR status comment.
    vcs_apply_blocked_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # VCS-side actor for comment-driven actions. Recorded directly (no Terrapod
    # identity mapping) — see "Authorization model" in #282.
    vcs_actor_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vcs_actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    plan_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    plan_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    apply_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    apply_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="runs")

    __table_args__ = (
        Index("ix_runs_workspace_id", "workspace_id"),
        Index("ix_runs_status", "status"),
    )


# --- Audit Logs ---


class AuditLog(Base):
    """Immutable audit log entry for API requests.

    Captures who did what, when, and the result. Sensitive data is
    redacted before persistence. Retained for a configurable number
    of days (default 90).
    """

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    actor_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    actor_ip: Mapped[str] = mapped_column(String(45), nullable=False, default="")
    action: Mapped[str] = mapped_column(
        String(40), nullable=False
    )  # HTTP method for API events; verb (apply/plan/merge/...) for VCS events
    resource_type: Mapped[str] = mapped_column(String(63), nullable=False, default="")
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(String(63), nullable=False, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Dual-actor model (#282).
    # - actor_type: "terrapod_user" (HTTP/UI/API), "vcs_user" (PR comment),
    #   "system" (background tasks).
    # - origin: "api", "terrapod_ui", "pr_comment", "system".
    # - actor_login: VCS-side display login (e.g. GitHub username) when
    #   actor_type is vcs_user; empty otherwise.
    # - actor_id: provider-side immutable user id (e.g. GitHub user id) when
    #   actor_type is vcs_user; empty otherwise.
    actor_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="terrapod_user"
    )
    origin: Mapped[str] = mapped_column(String(20), nullable=False, server_default="api")
    actor_login: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        Index("ix_audit_logs_timestamp", "timestamp"),
        Index("ix_audit_logs_actor_email", "actor_email"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
        Index("ix_audit_logs_actor_type", "actor_type"),
    )


# --- Run Triggers ---


class RunTrigger(Base):
    """Cross-workspace run trigger.

    When the source workspace completes an apply, the destination workspace
    automatically gets a new run queued. No data is passed — downstream
    workspaces read outputs via terraform_remote_state independently.
    """

    __tablename__ = "run_triggers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    workspace: Mapped["Workspace"] = relationship(foreign_keys=[workspace_id])
    source_workspace: Mapped["Workspace"] = relationship(foreign_keys=[source_workspace_id])

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "source_workspace_id", name="uq_run_triggers"),
        Index("ix_run_triggers_source_workspace_id", "source_workspace_id"),
    )


class WorkspaceRemoteStateConsumer(Base):
    """Producer-controlled cross-workspace state-read grant (#344).

    A row ``(producer_workspace_id, consumer_workspace_id)`` authorizes
    the consumer workspace's agent runs to read the producer
    workspace's state via ``terraform_remote_state``. Producer-owned:
    only the producer workspace's admin may create/delete a row. No
    rows for a producer ⇒ its state is not shared (secure by default).

    Independent of ``RunTrigger`` — neither implies the other (a run
    trigger is "re-run me when A applies"; this is "B may read A's
    state"). Mirrors the run-trigger edge shape; the deliberate
    difference is producer-side authorization.
    """

    __tablename__ = "workspace_remote_state_consumers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    producer_workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    consumer_workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    producer_workspace: Mapped["Workspace"] = relationship(foreign_keys=[producer_workspace_id])
    consumer_workspace: Mapped["Workspace"] = relationship(foreign_keys=[consumer_workspace_id])

    __table_args__ = (
        sa.UniqueConstraint(
            "producer_workspace_id",
            "consumer_workspace_id",
            name="uq_workspace_remote_state_consumers",
        ),
        Index("ix_wrsc_consumer_workspace_id", "consumer_workspace_id"),
        Index("ix_wrsc_producer_workspace_id", "producer_workspace_id"),
    )


# --- Notification Configurations ---


class NotificationConfiguration(Base):
    """Workspace-scoped notification configuration.

    Fires notifications on run lifecycle events to generic webhooks,
    Slack channels, or email addresses.
    """

    __tablename__ = "notification_configurations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    destination_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # generic, slack, email
    url: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    token: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    triggers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=list)
    email_addresses: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=list)
    delivery_responses: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    workspace: Mapped["Workspace"] = relationship()

    __table_args__ = (Index("ix_notification_configurations_workspace_id", "workspace_id"),)


# --- Run Tasks ---


class RunTask(Base):
    """Workspace-scoped run task definition.

    Configures a webhook hook for external validation at a specific stage
    of the run lifecycle (pre_plan, post_plan, pre_apply).
    """

    __tablename__ = "run_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    hmac_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    stage: Mapped[str] = mapped_column(String(20), nullable=False)  # pre_plan, post_plan, pre_apply
    enforcement_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="mandatory"
    )  # mandatory, advisory

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    workspace: Mapped["Workspace"] = relationship()

    __table_args__ = (Index("ix_run_tasks_workspace_id", "workspace_id"),)


class TaskStage(Base):
    """Per-run stage execution instance.

    Created when a run reaches a stage boundary that has applicable run tasks.
    Tracks overall pass/fail for the stage.
    """

    __tablename__ = "task_stages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending"
    )  # pending/running/passed/failed/errored/canceled/overridden

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    results: Mapped[list["TaskStageResult"]] = relationship(
        back_populates="task_stage", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_task_stages_run_id", "run_id"),)


class TaskStageResult(Base):
    """Individual task result within a stage.

    Each result corresponds to one RunTask webhook call. The external
    service reports pass/fail via the callback endpoint using the
    callback_token.
    """

    __tablename__ = "task_stage_results"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    task_stage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("task_stages.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("run_tasks.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending"
    )  # pending/running/passed/failed/errored/unreachable
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    callback_token: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    task_stage: Mapped["TaskStage"] = relationship(back_populates="results")
    run_task: Mapped["RunTask | None"] = relationship()

    __table_args__ = (Index("ix_task_stage_results_task_stage_id", "task_stage_id"),)


class PolicySet(Base):
    """A named collection of OPA/Rego policies evaluated during a run (#343).

    Scoping reuses the label-RBAC allow/deny model (same shape as Role):
    a policy set applies to a workspace when ``global_scope`` is true, or
    when the workspace's labels/name match the allow rules and don't match
    the deny rules. There are no org/team/project concepts.

    Policies must be written in Rego v1 (OPA 1.x) — Terrapod is a new
    project with no legacy policies to support.
    """

    __tablename__ = "policy_sets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enforcement_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="advisory"
    )  # advisory, mandatory
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Scoping. global_scope=True → applies to every workspace. Otherwise the
    # label-RBAC allow/deny rules below decide (mirrors Role).
    global_scope: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    allow_names: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    deny_labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    deny_names: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    # Source discriminator. "inline" = policies managed via API/UI CRUD (default).
    # "vcs" = policies synced from a git repo; inline CRUD is rejected.
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="inline", default="inline"
    )  # inline, vcs

    # VCS configuration (populated when source=vcs)
    vcs_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("vcs_connections.id", ondelete="SET NULL"),
        nullable=True,
    )
    vcs_connection: Mapped["VCSConnection | None"] = relationship(
        "VCSConnection", foreign_keys=[vcs_connection_id], lazy="joined"
    )
    vcs_repo_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    vcs_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    policy_path: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    vcs_last_commit_sha: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    vcs_last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    vcs_last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_by: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    policies: Mapped[list["Policy"]] = relationship(
        back_populates="policy_set", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_policy_sets_name", "name"),)


class Policy(Base):
    """A single Rego policy belonging to a policy set (#343).

    The Rego source is stored inline — policies are small text documents,
    so there is no object-storage round trip.
    """

    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    policy_set_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policy_sets.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rego: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    policy_set: Mapped["PolicySet"] = relationship(back_populates="policies")

    __table_args__ = (
        sa.UniqueConstraint("policy_set_id", "name", name="uq_policies_set_name"),
        Index("ix_policies_policy_set_id", "policy_set_id"),
    )


class PolicyEvaluation(Base):
    """The outcome of evaluating one policy set against one run (#343).

    One row per (run, policy set). ``result`` holds the per-policy detail
    (pass/fail, deny messages). ``policy_set_id`` is SET NULL on policy-set
    deletion so the evaluation history survives; ``policy_set_name`` is a
    snapshot kept for display in that case.
    """

    __tablename__ = "policy_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    policy_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policy_sets.id", ondelete="SET NULL"),
        nullable=True,
    )
    policy_set_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # Enforcement level snapshotted at evaluation time — a later edit of the
    # policy set must not retroactively change a recorded run's gating.
    enforcement_level: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)  # passed, failed, errored
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    overridden_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    overridden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint("run_id", "policy_set_id", name="uq_policy_evaluations_run_set"),
        Index("ix_policy_evaluations_run_id", "run_id"),
        Index("ix_policy_evaluations_policy_set_id", "policy_set_id"),
    )


class PlanSummary(Base):
    """LLM-generated summary attached to a plan (#401).

    One-to-one with Run, keyed by (run_id). The summariser fires once
    when the plan phase reaches a terminal state and stores the result
    here, then emits a `plan_summary_ready` SSE event. Stored separately
    from `runs` so the column footprint of a 50-million-row table
    doesn't grow for a feature only some deployments enable, and so the
    (potentially large) description text doesn't bloat cold reads.

    `kind` distinguishes two skills the API runs:
      - "plan_summary": successful plan → describe proposed changes
        and rate risk. Input: PLAN_JSON + code.
      - "failure_analysis": errored plan → explain why it failed and
        suggest fixes. Input: plan log + code.

    Field reuse across kinds:
      - description: change summary OR failure explanation.
      - risk_level: change-risk severity OR failure severity.
      - risk_factors: discrete risks ({severity,title,detail,address})
        OR suggested fixes (same shape — severity = "how critical to
        apply this fix"). The UI renders them differently per kind.

    Status values:
      - "pending": handler started, no result yet (transient)
      - "ready": model returned a parseable structured response
      - "skipped": daily budget hit, or workspace mode == "disabled"
      - "errored": model call failed (HTTP / parse / refusal)

    The handler upserts on (run_id) so retries on the same run are
    idempotent — a "ready" row is never overwritten by a later errored
    attempt for the same plan.
    """

    __tablename__ = "plan_summaries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )

    kind: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="plan_summary", default="plan_summary"
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    # Model fields — populated when status == "ready"
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risk_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default=""
    )  # "low", "medium", "high", "critical", "" for non-ready
    risk_factors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)

    # Telemetry / debugging
    model: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    __table_args__ = (sa.UniqueConstraint("run_id", name="uq_plan_summaries_run"),)


class PlanSummaryMessage(Base):
    """One turn in the AI plan-summary chat thread (#463).

    Attached to a PlanSummary; the initial structured summary stays on
    the parent `PlanSummary` row (description + risk_factors). This
    table only stores conversational follow-ups — user questions and
    assistant replies — that build on top of that initial summary.

    Roles:
      - "user": operator follow-up question
      - "assistant": model reply

    Each assistant row carries its own telemetry (model, token counts,
    error) so the daily-budget gate debits per turn and an operator
    can audit cost across the thread. User rows have zero tokens; the
    columns exist so the table shape is uniform.

    Ordering is by `(plan_summary_id, created_at)` — the SDK / API
    returns rows in chronological order. uuid7 IDs are time-ordered
    too, so PK order matches `created_at` order for any sane clock.
    """

    __tablename__ = "plan_summary_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    plan_summary_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plan_summaries.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Telemetry — meaningful on assistant rows, zero on user rows.
    model: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_plan_summary_messages_role",
        ),
        sa.Index(
            "ix_plan_summary_messages_plan_created",
            "plan_summary_id",
            "created_at",
        ),
    )
