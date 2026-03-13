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


def utc_now() -> datetime:
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
        DateTime(timezone=True), default=utc_now, nullable=False
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
        DateTime(timezone=True), default=utc_now, nullable=False
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
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="user"
    )  # "user", "organization"
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    lifespan_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (Index("ix_api_tokens_user_email", "user_email"),)


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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    tokens: Mapped[list["AgentPoolToken"]] = relationship(
        back_populates="pool", passive_deletes=True
    )
    listeners: Mapped[list["RunnerListener"]] = relationship(
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    pool: Mapped["AgentPool"] = relationship(back_populates="tokens")

    __table_args__ = (Index("ix_agent_pool_tokens_pool_id", "pool_id"),)


class RunnerListener(Base):
    """A registered runner listener (local or remote).

    Durable identity only. Runtime state (online/offline, capacity,
    heartbeat) lives in Redis with tf:listener:{id}: prefix.
    """

    __tablename__ = "runner_listeners"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid7
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_pools.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    certificate_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    certificate_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    runner_definitions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    pool: Mapped["AgentPool"] = relationship(back_populates="listeners")

    __table_args__ = (Index("ix_runner_listeners_pool_id", "pool_id"),)


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
    )  # local, remote, agent
    auto_apply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    execution_backend: Mapped[str] = mapped_column(
        String(20), nullable=False, default="tofu"
    )  # tofu, terraform
    terraform_version: Mapped[str] = mapped_column(String(20), nullable=False, default="1.9")
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
    vcs_repo_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    vcs_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    vcs_working_directory: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    vcs_last_commit_sha: Mapped[str] = mapped_column(String(40), nullable=False, default="")

    # tfvars files — passed to runner as -var-file arguments
    var_files: Mapped[list[str]] = mapped_column(
        ARRAY(String(500)), nullable=False, server_default="{}"
    )

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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
    md5: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    state_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="state_versions")

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "serial", name="uq_state_versions"),
        Index("ix_state_versions_workspace_id", "workspace_id"),
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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    versions: Mapped[list["RegistryModuleVersion"]] = relationship(
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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    module: Mapped["RegistryModule"] = relationship(back_populates="versions")

    __table_args__ = (
        sa.UniqueConstraint("module_id", "version", name="uq_registry_module_versions"),
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
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
        DateTime(timezone=True), default=utc_now, nullable=False
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
        DateTime(timezone=True), default=utc_now, nullable=False
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
    ca_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
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

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )  # active, suspended, removed

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "provider", "github_installation_id", name="uq_vcs_connections_install"
        ),
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
        DateTime(timezone=True), default=utc_now, nullable=False
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
    resource_cpu: Mapped[str] = mapped_column(String(20), nullable=False, default="1")
    resource_memory: Mapped[str] = mapped_column(String(20), nullable=False, default="2Gi")
    pool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_pools.id", ondelete="SET NULL"),
        nullable=True,
    )
    listener_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("runner_listeners.id", ondelete="SET NULL"),
        nullable=True,
    )
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Run options (CLI flags)
    target_addrs: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    replace_addrs: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    refresh_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    refresh: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_empty_apply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Drift detection
    is_drift_detection: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_changes: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Job tracking (populated by listener after launching K8s Job)
    job_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_namespace: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # VCS metadata
    vcs_commit_sha: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    vcs_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    vcs_pull_request_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    actor_email: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    actor_ip: Mapped[str] = mapped_column(String(45), nullable=False, default="")
    action: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # HTTP method: GET, POST, PATCH, DELETE
    resource_type: Mapped[str] = mapped_column(String(63), nullable=False, default="")
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(String(63), nullable=False, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    __table_args__ = (
        Index("ix_audit_logs_timestamp", "timestamp"),
        Index("ix_audit_logs_actor_email", "actor_email"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    workspace: Mapped["Workspace"] = relationship(foreign_keys=[workspace_id])
    source_workspace: Mapped["Workspace"] = relationship(foreign_keys=[source_workspace_id])

    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "source_workspace_id", name="uq_run_triggers"),
        Index("ix_run_triggers_source_workspace_id", "source_workspace_id"),
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    task_stage: Mapped["TaskStage"] = relationship(back_populates="results")
    run_task: Mapped["RunTask | None"] = relationship()

    __table_args__ = (Index("ix_task_stage_results_task_stage_id", "task_stage_id"),)
