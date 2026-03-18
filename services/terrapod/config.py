"""
Configuration management for Terrapod API server.

Non-secret configuration loaded from YAML file, secrets from environment variables.
"""

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


def yaml_config_settings_source() -> dict[str, Any]:
    """Load configuration from YAML file."""
    config_path = Path("/etc/terrapod/config.yaml")
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


# --- Storage Configuration Models ---


# --- Runner Configuration Models ---


class RunnerResourceSpec(BaseModel):
    """CPU and memory specification for a runner Job."""

    cpu: str = Field(default="500m")
    memory: str = Field(default="1Gi")


class RunnerResources(BaseModel):
    """Resource requests and limits for a runner Job."""

    requests: RunnerResourceSpec = Field(default_factory=RunnerResourceSpec)
    limits: RunnerResourceSpec = Field(
        default_factory=lambda: RunnerResourceSpec(cpu="1", memory="2Gi")
    )


class RunnerDefinition(BaseModel):
    """Named runner definition — defines a K8s Job resource envelope."""

    name: str = Field(description="Runner definition name")
    description: str = Field(default="")
    resources: RunnerResources = Field(default_factory=RunnerResources)
    timeout_minutes: int = Field(default=60)
    setup_script: str = Field(
        default="",
        description="Shell script to run before terraform init (e.g. install providers, configure backends)",
    )


class RunnerImageConfig(BaseModel):
    """Container image used for runner Jobs."""

    repository: str = Field(default="ghcr.io/mattrobinsonsre/terrapod-runner")
    tag: str = Field(default="")
    pull_policy: str = Field(default="IfNotPresent")


class RunnerConfig(BaseModel):
    """Runner configuration, loaded from /etc/terrapod/runners.yaml.

    Separate from main Settings because listeners need their own
    runner definitions independent of the API server's config.
    """

    default: str = Field(default="standard", description="Default runner definition name")
    image: RunnerImageConfig = Field(default_factory=RunnerImageConfig)
    server_url: str = Field(
        default="",
        description="Internal API URL for runner Jobs (e.g. http://terrapod-api:8000). "
        "Used as base URL for presigned storage URLs. Falls back to TERRAPOD_API_URL env var.",
    )
    default_terraform_version: str = Field(default="1.9.8")
    default_execution_backend: str = Field(default="terraform")
    service_account_name: str = Field(default="")
    azure_workload_identity: bool = Field(default=False)
    ttl_seconds_after_finished: int = Field(default=600)
    definitions: list[RunnerDefinition] = Field(
        default_factory=lambda: [RunnerDefinition(name="standard", description="Standard runner")]
    )
    termination_grace_period_seconds: int = Field(
        default=120,
        description="Time budget for graceful shutdown + artifact uploads (pod terminationGracePeriodSeconds)",
    )
    token_ttl_seconds: int = Field(default=3600, description="Default runner token TTL")
    max_token_ttl_seconds: int = Field(default=7200, description="Maximum runner token TTL")
    node_selector: dict[str, str] = Field(default_factory=dict)
    tolerations: list[dict] = Field(default_factory=list)
    affinity: dict = Field(default_factory=dict)
    pod_annotations: dict[str, str] = Field(default_factory=dict)
    priority_class_name: str = Field(default="")
    topology_spread_constraints: list[dict] = Field(default_factory=list)
    pod_security_context: dict = Field(default_factory=dict)


def load_runner_config(path: str = "/etc/terrapod/runners.yaml") -> RunnerConfig:
    """Load runner configuration from YAML file."""
    config_path = Path(path)
    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
        return RunnerConfig(**data)
    return RunnerConfig()


# --- Storage Configuration Models ---


class StorageBackend(StrEnum):
    """Supported storage backends."""

    S3 = "s3"
    AZURE = "azure"
    GCS = "gcs"
    FILESYSTEM = "filesystem"


class S3Config(BaseModel):
    """AWS S3 storage configuration."""

    bucket: str = Field(default="", description="S3 bucket name")
    region: str = Field(default="us-east-1", description="AWS region")
    prefix: str = Field(default="", description="Key prefix within the bucket")
    endpoint_url: str = Field(
        default="",
        description="Custom endpoint URL (for LocalStack in dev/CI)",
    )
    presigned_url_expiry_seconds: int = Field(
        default=3600,
        description="Presigned URL expiry in seconds. Must not exceed IRSA credential lifetime (~1h)",
    )


class AzureConfig(BaseModel):
    """Azure Blob Storage configuration."""

    account_name: str = Field(default="", description="Storage account name")
    container_name: str = Field(default="", description="Blob container name")
    prefix: str = Field(default="", description="Key prefix within the container")
    presigned_url_expiry_seconds: int = Field(
        default=3600,
        description="SAS URL expiry in seconds",
    )


class GCSConfig(BaseModel):
    """Google Cloud Storage configuration."""

    bucket: str = Field(default="", description="GCS bucket name")
    prefix: str = Field(default="", description="Key prefix within the bucket")
    project_id: str = Field(default="", description="GCP project ID")
    service_account_email: str = Field(
        default="",
        description="Service account email for signed URLs (auto-detected if empty)",
    )
    presigned_url_expiry_seconds: int = Field(
        default=3600,
        description="Signed URL expiry in seconds",
    )


class FilesystemConfig(BaseModel):
    """Local filesystem storage configuration."""

    root_dir: str = Field(
        default="/var/lib/terrapod/storage",
        description="Root directory for file storage",
    )
    presigned_url_expiry_seconds: int = Field(
        default=3600,
        description="HMAC-signed URL expiry in seconds",
    )
    hmac_secret: str = Field(
        default="",
        description="HMAC secret for signing URLs. Generated at startup if empty.",
    )
    base_url: str = Field(
        default="http://localhost:8000",
        description="Base URL for presigned URL generation",
    )


class StorageConfig(BaseModel):
    """Storage configuration."""

    backend: StorageBackend = Field(
        default=StorageBackend.FILESYSTEM,
        description="Storage backend: s3, azure, gcs, or filesystem",
    )
    s3: S3Config = Field(default_factory=S3Config)
    azure: AzureConfig = Field(default_factory=AzureConfig)
    gcs: GCSConfig = Field(default_factory=GCSConfig)
    filesystem: FilesystemConfig = Field(default_factory=FilesystemConfig)


# --- SSO Configuration Models ---


class ClaimsToRolesMapping(BaseModel):
    """Maps an IDP claim value to Terrapod roles."""

    claim: str = Field(description="Claim name (e.g., 'groups', 'https://myorg.com/roles')")
    value: str = Field(description="Claim value to match")
    roles: list[str] = Field(description="Terrapod roles to assign when matched")


class OIDCProviderConfig(BaseModel):
    """Configuration for a single OIDC identity provider."""

    name: str = Field(description="Unique provider name (e.g., 'auth0', 'okta')")
    display_name: str = Field(
        default="", description="Human-readable label for login UI (falls back to name)"
    )
    issuer_url: str = Field(description="OIDC issuer URL for discovery")
    client_id: str = Field(description="OAuth2 client ID")
    client_secret: str = Field(default="", description="OAuth2 client secret (from env)")
    scopes: list[str] = Field(
        default=["openid", "profile", "email"],
        description="OAuth2 scopes to request",
    )
    audience: str = Field(
        default="",
        description="API audience (resource server identifier). When set, included in authorize "
        "request and permissions are extracted from the access token.",
    )
    groups_claim: str = Field(
        default="groups",
        description="Claim name containing user groups",
    )
    role_prefixes: list[str] = Field(
        default=["terrapod:", "terrapod-"],
        description="Prefixes to strip from group names to derive role names.",
    )
    claims_to_roles: list[ClaimsToRolesMapping] = Field(
        default_factory=list,
        description="Rules mapping IDP claims to Terrapod roles",
    )


class SAMLProviderConfig(BaseModel):
    """Configuration for a single SAML identity provider."""

    name: str = Field(description="Unique provider name (e.g., 'azure-ad')")
    display_name: str = Field(
        default="", description="Human-readable label for login UI (falls back to name)"
    )
    metadata_url: str = Field(description="IDP metadata URL")
    entity_id: str = Field(default="", description="SP entity ID")
    acs_url: str = Field(default="", description="Assertion consumer service URL")
    role_prefixes: list[str] = Field(
        default=["terrapod:", "terrapod-"],
        description="Prefixes to strip from group names to derive role names.",
    )
    claims_to_roles: list[ClaimsToRolesMapping] = Field(
        default_factory=list,
        description="Rules mapping SAML attributes to Terrapod roles",
    )


class SSOConfig(BaseModel):
    """SSO configuration with multiple providers."""

    default_provider: str = Field(
        default="",
        description="Default provider when no --provider given",
    )
    oidc: list[OIDCProviderConfig] = Field(
        default_factory=list,
        description="OIDC identity providers",
    )
    saml: list[SAMLProviderConfig] = Field(
        default_factory=list,
        description="SAML identity providers",
    )


class AuthConfig(BaseSettings):
    """Authentication configuration."""

    local_enabled: bool = Field(default=True, description="Enable local username/password auth")
    callback_base_url: str = Field(
        default="http://localhost:8000",
        description="Base URL for IDP callbacks (externally-reachable URL)",
    )
    sso: SSOConfig = Field(default_factory=SSOConfig)
    session_ttl_hours: int = Field(
        default=12,
        description="Session TTL in hours",
    )
    api_token_max_ttl_hours: int = Field(
        default=8760,
        description="Maximum API token lifetime in hours (default: 8760 = 1 year). "
        "0 = no limit. Computed at validation time as created_at + this value.",
    )
    require_external_sso_for_roles: list[str] = Field(
        default_factory=list,
        description="Roles that require external SSO login (excludes local provider)",
    )


# --- Audit Configuration ---


class AuditConfig(BaseModel):
    """Audit logging configuration."""

    retention_days: int = Field(
        default=90,
        description="Number of days to retain audit log entries. Entries older than this are deleted by the retention task.",
    )


# --- Notifications Configuration ---


class SMTPConfig(BaseModel):
    """SMTP configuration for email notifications."""

    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    from_address: str = "notifications@terrapod.local"
    use_tls: bool = True


class NotificationsConfig(BaseModel):
    """Notification delivery configuration."""

    enabled: bool = True
    delivery_timeout_seconds: int = 30
    max_delivery_responses: int = 10
    smtp: SMTPConfig = Field(default_factory=SMTPConfig)


# --- Registry Configuration ---


class ProviderCacheConfig(BaseModel):
    """Provider binary caching (network mirror) configuration."""

    enabled: bool = Field(default=True)
    upstream_registries: list[str] = Field(
        default=["registry.terraform.io", "registry.opentofu.org"]
    )
    warm_on_first_request: bool = Field(default=True)


class BinaryCacheConfig(BaseModel):
    """Terraform/tofu CLI binary caching configuration."""

    enabled: bool = Field(default=True)
    terraform_mirror_url: str = Field(default="https://releases.hashicorp.com/terraform")
    tofu_mirror_url: str = Field(default="https://github.com/opentofu/opentofu/releases/download")


class RegistryConfig(BaseModel):
    """Private registry and caching configuration."""

    enabled: bool = Field(default=True)
    signing_key: str = Field(
        default="",
        description="ASCII-armored GPG private key for provider signing. "
        "If set, imported on first use and used to sign all provider SHA256SUMS. "
        "If empty, a key is auto-generated on first provider upload. "
        "Set via TERRAPOD_REGISTRY__SIGNING_KEY env var or K8s Secret.",
    )
    provider_cache: ProviderCacheConfig = Field(default_factory=ProviderCacheConfig)
    binary_cache: BinaryCacheConfig = Field(default_factory=BinaryCacheConfig)


# --- VCS Configuration ---


class GitHubWebhookConfig(BaseModel):
    """GitHub webhook configuration (optional, for faster feedback)."""

    webhook_secret: str = Field(
        default="",
        description="Webhook secret for HMAC signature validation (optional)",
    )


class VCSConfig(BaseModel):
    """VCS integration configuration."""

    enabled: bool = Field(default=True, description="Enable VCS integration")
    poll_interval_seconds: int = Field(
        default=60, description="Polling interval in seconds for VCS changes"
    )
    github: GitHubWebhookConfig = Field(default_factory=GitHubWebhookConfig)


# --- Drift Detection Configuration ---


class DriftDetectionConfig(BaseModel):
    """Drift detection configuration.

    When enabled, a periodic scheduler task checks all drift-enabled workspaces
    and creates plan-only runs to detect infrastructure drift.
    """

    enabled: bool = Field(default=False, description="Enable drift detection")
    poll_interval_seconds: int = Field(
        default=300,
        description="How often the scheduler checks for workspaces due for drift detection",
    )
    min_workspace_interval_seconds: int = Field(
        default=3600,
        description="Minimum per-workspace drift check interval (floor, 1 hour)",
    )


# --- CORS Configuration ---


class CORSConfig(BaseModel):
    """CORS (Cross-Origin Resource Sharing) configuration."""

    allow_origins: list[str] = Field(
        default_factory=list,
        description="Allowed origins. Empty list means CORS middleware is disabled.",
    )
    allow_credentials: bool = Field(
        default=True, description="Allow credentials (cookies, auth headers)"
    )
    allow_methods: list[str] = Field(
        default=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        description="Allowed HTTP methods",
    )
    allow_headers: list[str] = Field(
        default=["Content-Type", "Authorization", "X-Request-ID"],
        description="Allowed request headers",
    )


# --- Rate Limiting Configuration ---


class RateLimitConfig(BaseModel):
    """API rate limiting configuration."""

    enabled: bool = Field(default=False, description="Enable rate limiting")
    requests_per_minute: int = Field(
        default=100, description="Max requests per minute for general API endpoints"
    )
    auth_requests_per_minute: int = Field(
        default=10, description="Max requests per minute for auth endpoints"
    )


# --- Metrics Configuration ---


class MetricsConfig(BaseModel):
    """Prometheus metrics configuration."""

    enabled: bool = Field(
        default=True, description="Expose /metrics endpoint and instrument requests"
    )


# --- Main Settings ---


class Settings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_prefix="TERRAPOD_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Application
    app_name: str = Field(default="terrapod-api")
    version: str = Field(
        default="",
        description="Platform version (injected from Chart.AppVersion at deploy time)",
    )
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    json_logs: bool = Field(default=True, description="JSON logging in production")
    external_url: str = Field(
        default="",
        description="Public-facing Terrapod URL (e.g. https://terrapod.example.com). "
        "Used for commit status links, notification URLs, and any outbound links back to the UI.",
    )

    # Database
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://terrapod:terrapod@localhost:5432/terrapod",
        description="PostgreSQL connection URL",
    )

    # Redis
    redis_url: RedisDsn = Field(
        default="redis://localhost:6379",
        description="Redis connection URL",
    )

    # Storage
    storage: StorageConfig = Field(default_factory=StorageConfig)

    # Authentication
    auth: AuthConfig = Field(default_factory=AuthConfig)

    # Audit
    audit: AuditConfig = Field(default_factory=AuditConfig)

    # Registry
    registry: RegistryConfig = Field(default_factory=RegistryConfig)

    # VCS
    vcs: VCSConfig = Field(default_factory=VCSConfig)

    # Notifications
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    # Drift Detection
    drift_detection: DriftDetectionConfig = Field(default_factory=DriftDetectionConfig)

    # CORS
    cors: CORSConfig = Field(default_factory=CORSConfig)

    # Rate Limiting
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)

    # Metrics
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)

    # Workspace defaults
    default_execution_backend: str = Field(
        default="tofu",
        description="Default execution backend for new workspaces (tofu or terraform)",
    )
    default_terraform_version: str = Field(
        default="1.9",
        description="Default terraform/tofu version for new workspaces",
    )

    # API
    api_prefix: str = Field(default="/api/v2")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        """Customize settings sources: env vars override YAML config."""
        return (
            init_settings,
            env_settings,
            yaml_config_settings_source,
            dotenv_settings,
            file_secret_settings,
        )


# Global settings instance
settings = Settings()
