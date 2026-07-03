"""
Configuration management for Terrapod API server.

Non-secret configuration loaded from YAML file, secrets from environment variables.
"""

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def yaml_config_settings_source() -> dict[str, Any]:
    """Load configuration from YAML file."""
    config_path = Path("/etc/terrapod/config.yaml")
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


def runners_yaml_config_settings_source() -> dict[str, Any]:
    """Load runner/listener configuration from the runner ConfigMap (runners.yaml).

    The listener mounts this ConfigMap at /etc/terrapod/runners.yaml. Mirrors
    `yaml_config_settings_source` so `RunnerConfig` layers defaults → file → env
    via pydantic-settings, instead of the listener hand-reading os.environ. The
    file is absent in the API pod (which only consumes a few runner defaults via
    `load_runner_config`), so it falls back to defaults + env there.
    """
    config_path = Path("/etc/terrapod/runners.yaml")
    if config_path.exists():
        with open(config_path) as f:
            return yaml.safe_load(f) or {}
    return {}


# --- Storage Configuration Models ---


# --- Runner Configuration Models ---


class RunnerImageConfig(BaseModel):
    """Container image used for runner Jobs."""

    repository: str = Field(default="ghcr.io/mattrobinsonsre/terrapod-runner")
    tag: str = Field(default="")
    pull_policy: str = Field(default="IfNotPresent")


class RunnerProxyConfig(BaseModel):
    """Forward-proxy settings forwarded into runner Jobs (#592).

    Rendered into runners.yaml from the chart's top-level .Values.proxy. The
    listener injects these as HTTP(S)_PROXY/NO_PROXY (upper + lower case) env
    vars on the runner Job so `terraform init` reaches PUBLIC registry/git
    module sources through a corporate proxy. no_proxy is pre-resolved by the
    chart (cluster-local defaults + operator list); we do NOT assume the
    runner→API hop is proxy-exempt — in split-cluster it traverses the proxy.
    """

    http_proxy: str = Field(default="")
    https_proxy: str = Field(default="")
    no_proxy: str = Field(default="")


class RunnerConfig(BaseSettings):
    """Runner + listener configuration, layered defaults → runners.yaml → env.

    A pydantic-settings model (like `Settings`) so the listener reads ONE
    config object instead of hand-reading os.environ: the runner ConfigMap
    (runners.yaml) is the file source, `TERRAPOD_*` env vars override it, and
    field defaults backstop both. This carries BOTH the runner-Job settings
    (image, resources, proxy/CA) AND the listener's own operational settings
    (name, namespaces, API URLs, health port, cert TTL, SSE/heartbeat/poll
    knobs) — all non-sensitive, so they flow through the ConfigMap, not via
    chart-set Deployment env vars. Secrets (the join token) and runtime values
    (POD_NAME) stay as Deployment env and are NOT fields here.
    """

    model_config = SettingsConfigDict(
        env_prefix="TERRAPOD_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    image: RunnerImageConfig = Field(default_factory=RunnerImageConfig)
    server_url: str = Field(
        default="",
        description="Internal API URL the listener + runner Jobs call "
        "(e.g. http://terrapod-api:8000). Also the base for presigned storage URLs. "
        "Env override: TERRAPOD_SERVER_URL.",
    )
    default_terraform_version: str = Field(default="1.12")
    default_execution_backend: str = Field(default="tofu")
    # --- Listener operational settings (non-sensitive; from runners.yaml) ---
    # Previously read by the listener directly from os.environ; now layered
    # through this config object so they are ConfigMap-driven, not chart-env.
    listener_name: str = Field(
        default="listener",
        description="Base listener name; the pod name is appended per replica.",
    )
    runner_namespace: str = Field(
        default="terrapod-runners",
        description="Namespace the listener creates runner Jobs + per-run Secrets in.",
    )
    listener_namespace: str = Field(
        default="terrapod",
        description="Fallback namespace for the listener's own resources when the "
        "in-pod service-account namespace file is unreadable (local/dev).",
    )
    credentials_secret_name: str = Field(
        default="",
        description="Explicit name for the listener credentials Secret. Empty → "
        "derived as '{listener_name}-credentials'. Set by the chart to tie the "
        "Secret to the Deployment lifecycle (a Secret NAME, not a secret value).",
    )
    health_port: int = Field(default=8081, description="Listener health/readiness port.")
    public_api_url: str = Field(
        default="",
        description="Public/canonical API URL forwarded to runner Jobs as "
        "TP_PUBLIC_API_URL when it differs from server_url (canonical→internal "
        "host redirect). Empty in single-network deployments.",
    )
    listener_cert_ttl_seconds: int = Field(
        default=3600,
        description="Listener certificate validity; drives the renewal threshold. "
        "Must match api.config.agent_pools.listener_cert_ttl_seconds.",
    )
    heartbeat_interval: int = Field(default=60, description="Listener heartbeat interval (s).")
    max_concurrent: int = Field(
        default=3, description="Max concurrent run launches per listener pod."
    )
    poll_interval: int = Field(
        default=30, description="Fallback poll interval when SSE is idle (s)."
    )
    sse_read_timeout: int = Field(
        default=30, description="SSE read timeout — silence beyond this reconnects (s)."
    )
    sse_max_age: int = Field(
        default=600, description="Max SSE connection age before a proactive reconnect (s)."
    )
    sse_retry_interval: int = Field(
        default=5, description="Backoff between SSE reconnect attempts (s)."
    )
    service_account_name: str = Field(default="")
    azure_workload_identity: bool = Field(default=False)
    ttl_seconds_after_finished: int = Field(default=600)
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
    image_pull_secrets: list[str] = Field(default_factory=list)
    extra_env: list[dict] = Field(default_factory=list)
    extra_env_from: list[dict] = Field(default_factory=list)
    # Forward proxy + custom CA trust bundle (#592). Rendered into runners.yaml
    # by the chart from top-level .Values.proxy / .Values.caBundle. The listener
    # forwards these into every runner Job: proxy env vars (so terraform init can
    # fetch PUBLIC registry/git modules through a corporate proxy) and the custom
    # CA (so the runner trusts a TLS-intercepting proxy / private registry).
    proxy: RunnerProxyConfig | None = Field(default=None)
    ca_bundle_enabled: bool = Field(default=False)
    ca_bundle_source_path: str = Field(
        default="",
        description=(
            "Path on the LISTENER pod to the raw custom CA file (mounted from "
            "the chart's caBundle source). The listener reads it at Job-launch "
            "time and ships it into a per-run Secret in the runner namespace, "
            "where the runner Job's init container merges it with the runner "
            "image's own system roots."
        ),
    )
    stale_timeout_seconds: int = Field(
        default=3600,
        description="Seconds before a run with no Job status is marked errored",
    )
    launch_timeout_seconds: int = Field(
        default=300,
        description=(
            "Seconds before a claimed run with NO job_name is marked errored. "
            "Distinct from stale_timeout: this catches the 'listener claimed "
            "the run but never launched a Job' failure mode (e.g. /runner-token "
            "401, K8s API outage at create_job time). Shorter default than "
            "stale_timeout because 'no Job launched after 5 min' is a clear "
            "failure signal, whereas a long-running Job with no status is not."
        ),
    )
    drift_max_duration_seconds: int = Field(
        default=1800,
        description=(
            "Maximum wall-clock seconds a drift-detection run may spend in "
            "`planning` before the reconciler errors it out and frees the "
            "workspace for the next drift cycle. Distinct from stale_timeout: "
            "stale_timeout protects against listeners that stop reporting; "
            "this protects against terraform legitimately running for hours "
            "(e.g. a github provider refresh on a workspace with hundreds of "
            "rate-limited reads) and blocking drift indefinitely for that "
            "workspace. Drift runs are background-priority and plan-only — "
            "a 30 min cap is a generous SLO for 'just tell me if there's "
            "drift'. Set higher for deployments with genuinely large plans "
            "you still want drift on; set to 0 to disable the cap."
        ),
    )
    lifecycle_destroy_retries: int = Field(
        default=2,
        description=(
            "How many times to automatically retry a FAILED platform-initiated "
            "lifecycle destroy — a catalog instance destroy (source "
            "`catalog-lifecycle`) or an autodiscovery directory destroy (source "
            "`autodiscovery-lifecycle`) — before leaving it errored for an "
            "operator. terraform destroy is commonly transiently flaky "
            "(dependency-release ordering, eventual consistency, draining load "
            "balancers / releasing ENIs / emptying buckets) and re-running is "
            "safe because destroy is declarative and incremental. The workspace "
            "is only archived on a SUCCESSFUL destroy, so retries never lose "
            "data: they either eventually succeed and archive, or exhaust the "
            "cap and stay errored. A user's own CLI `terraform destroy` is "
            "never auto-retried. Set to 0 to disable. Default 2 (3 attempts)."
        ),
    )
    lifecycle_destroy_retry_backoff_seconds: int = Field(
        default=45,
        description=(
            "Seconds to wait after a lifecycle-destroy run errors before "
            "queuing the next retry, giving transient dependencies time to "
            "settle before terraform tries the teardown again."
        ),
    )
    hooks_enabled: bool = Field(
        default=True,
        description=(
            "Kill-switch for execution hooks (#619). When false, the listener "
            "drops all hooks resolved for a run before building the Job, so no "
            "operator shell runs in the runner. For security-conscious / sealed "
            "deployments that want to forbid custom-shell hooks entirely."
        ),
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        """Layer sources: init args > env vars > runners.yaml > defaults."""
        return (
            init_settings,
            env_settings,
            runners_yaml_config_settings_source,
            dotenv_settings,
            file_secret_settings,
        )


def load_runner_config(path: str = "/etc/terrapod/runners.yaml") -> RunnerConfig:
    """Construct the runner/listener config (defaults → runners.yaml → env).

    The `path` argument is retained for backward compatibility but ignored: the
    runners.yaml file is now a pydantic-settings source on `RunnerConfig`
    (`runners_yaml_config_settings_source`, fixed at /etc/terrapod/runners.yaml),
    so the layering — and env overrides — happen inside the model.
    """
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
        description="Maximum interactive API token lifetime in hours (default: 8760 = 1 year). "
        "0 = no limit. Expiry = (rotated_at or created_at) + this value.",
    )
    service_token_max_ttl_hours: int = Field(
        default=8760,
        description="Maximum service-token (service_bound/service_detached) lifetime in hours "
        "(default: 8760 = 1 year). Separate from the interactive cap. Service tokens ALWAYS "
        "expire: this is never treated as unbounded even if set to 0 (#495).",
    )
    token_expiry_warning_days: int = Field(
        default=14,
        description="How many days ahead to surface in-app warnings for service tokens nearing "
        "expiry (#495).",
    )
    bound_token_idle_days: int = Field(
        default=7,
        description="Idle-login window in days for user-bound tokens (interactive + service_bound): "
        "a bound token is rejected if its owner has not logged in within this window. Also the TTL "
        "of the tp:user_seen marker. 0 disables idle rejection. Detached tokens are exempt (#495). "
        "ON by default — convert existing automation tokens to detached before upgrade.",
    )
    login_token_ttl_hours: int = Field(
        default=12,
        description="Lifespan in hours of the API token minted by `terraform login` (default: 12). "
        "These are short-lived interactive credentials for a human's CLI session, distinct from the "
        "api_token_max_ttl_hours cap (which is the upper bound on any token). Still clamped to that "
        "cap. 0 = no per-login lifespan (falls back to the cap).",
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


class WarmPlatform(BaseModel):
    """A single os/arch target for cache pre-population."""

    os: str
    arch: str


class WarmBinaryEntry(BaseModel):
    """A declarative binary-cache pre-population entry.

    Pulled into the cache by the post-install/upgrade warm Job and by the
    bulk-warm admin endpoint. An empty `platforms` list falls back to the
    default warm platforms (linux/amd64 + linux/arm64) so the common case is
    just {tool, version}.
    """

    tool: Literal["terraform", "tofu", "terragrunt"] = "terraform"
    version: str
    platforms: list[WarmPlatform] = Field(default_factory=list)


class WarmProviderEntry(BaseModel):
    """A declarative provider-cache pre-population entry.

    `source` is the provider address `hostname/namespace/type`
    (e.g. `registry.terraform.io/hashicorp/aws`). An empty `platforms` list
    falls back to `provider_cache.platforms`.
    """

    source: str
    version: str
    platforms: list[WarmPlatform] = Field(default_factory=list)

    @field_validator("source")
    @classmethod
    def _validate_source(cls, v: str) -> str:
        parts = v.split("/")
        if len(parts) != 3 or not all(p.strip() for p in parts):
            raise ValueError(
                "provider warm source must be 'hostname/namespace/type' "
                "(e.g. 'registry.terraform.io/hashicorp/aws')"
            )
        return v

    @property
    def coordinates(self) -> tuple[str, str, str]:
        hostname, namespace, type_ = self.source.split("/")
        return hostname, namespace, type_


class ProviderCacheConfig(BaseModel):
    """Provider binary caching (network mirror) configuration."""

    enabled: bool = Field(default=True)
    upstream_registries: list[str] = Field(
        default=["registry.terraform.io", "registry.opentofu.org"]
    )
    warm_on_first_request: bool = Field(default=True)
    platforms: list[dict[str, str]] = Field(
        default=[{"os": "linux", "arch": "amd64"}, {"os": "linux", "arch": "arm64"}],
    )
    verify: Literal["off", "checksum", "signature"] = Field(
        default="signature",
        description="Integrity verification for provider archives fetched from upstream. "
        "'signature' (default, fail-closed): verify the archive's SHA-256 against the "
        "registry-advertised shasum AND verify the registry's SHA256SUMS GPG signature "
        "against the registry-advertised signing key (mirrors `terraform init`). "
        "'checksum': verify the SHA-256 against the advertised shasum only. "
        "'off': no verification (NOT recommended — the mirror would trust any bytes). "
        "On failure the fetch is rejected; nothing is cached or served.",
    )
    allow_unsigned: bool = Field(
        default=False,
        description="In 'signature' mode, what to do when the upstream advertises NO "
        "signature material (some private registries / non-signing network mirrors, "
        "and some community providers). Default false = fail closed (strict). Set true "
        "to gracefully degrade to a shasum-only check (with a warning) for those "
        "providers instead of rejecting them — the archive checksum is still verified "
        "against the advertised shasum. Only relevant when verify='signature'.",
    )


class BinaryCacheConfig(BaseModel):
    """Terraform/tofu CLI binary caching configuration."""

    enabled: bool = Field(default=True)
    terraform_mirror_url: str = Field(default="https://releases.hashicorp.com/terraform")
    tofu_mirror_url: str = Field(default="https://github.com/opentofu/opentofu/releases/download")
    terragrunt_mirror_url: str = Field(
        default="https://github.com/gruntwork-io/terragrunt/releases/download",
        description="Upstream download base for terragrunt binaries (GitHub releases). "
        "Terragrunt ships a bare per-platform binary asset (terragrunt_<os>_<arch>), "
        "not a zip/tarball — the pull-through cache stores it as-is. Only used by "
        "workspaces with terragrunt enabled.",
    )
    # --- Version-index (listing / partial-version resolution) sources ---
    # The *_mirror_url fields above are the per-binary download bases. These are
    # the separate endpoints the cache reads to LIST available versions and to
    # resolve a partial version (e.g. "1.12" -> "1.12.3"). Upstream they live on
    # different hosts from the download base (terraform's index is on
    # releases.hashicorp.com, tofu's on get.opentofu.org, terragrunt's on the
    # GitHub API), so an internal-mirror / air-gapped deployment must override
    # BOTH the download base and the version-index source for each tool. A mirror
    # MUST serve the same response shape as the upstream it replaces (documented
    # per field below), since the cache parses the upstream JSON format.
    terraform_version_index_url: str = Field(
        default="https://releases.hashicorp.com/terraform/index.json",
        description="Version-index source for terraform. Must return the HashiCorp "
        "releases index JSON shape: a top-level object with a `versions` map keyed "
        'by version string (e.g. {"versions": {"1.12.3": {...}}}). Used for '
        "version listing and partial-version resolution.",
    )
    tofu_version_index_url: str = Field(
        default="https://get.opentofu.org/tofu/api.json",
        description="Version-index source for tofu. Must return the OpenTofu index "
        'JSON shape: {"versions": [{"id": "1.12.3"}, ...]} with ids carrying no '
        "leading 'v'. (Distinct from tofu_mirror_url, which is the GitHub releases "
        "download base.) Used for version listing and partial-version resolution.",
    )
    terragrunt_version_index_url: str = Field(
        default="https://api.github.com/repos/gruntwork-io/terragrunt/releases",
        description="Version-index source for terragrunt. Must return the GitHub "
        "releases API shape: a JSON array of objects with `tag_name` (e.g. "
        '"v0.58.0") and `prerelease`. Used for version listing and '
        "partial-version resolution.",
    )
    allow_prerelease: Literal["none", "rc", "beta", "alpha", "dev"] = Field(
        default="none",
        description="Lowest pre-release tier to accept for terraform/tofu/terragrunt CLI version "
        "resolution and caching. The value names the LEAST stable tier allowed; every "
        "more-stable tier is also allowed. 'none' (default) permits only GA releases. "
        "'rc' allows release candidates and GA. 'dev' allows everything. "
        "Intended for dev/staging deployments trying upcoming releases such as "
        "terraform 1.15-rc or tofu 1.12-beta.",
    )
    verify: Literal["off", "checksum", "signature"] = Field(
        default="signature",
        description="Integrity verification for terraform/tofu/terragrunt binaries fetched "
        "from upstream. 'signature' (default, fail-closed): verify the upstream SHA256SUMS "
        "GPG signature against the pinned publisher key (HashiCorp / OpenTofu / Gruntwork) "
        "AND verify the downloaded binary's SHA-256 against that signed manifest. "
        "'checksum': verify against the SHA256SUMS manifest only (no signature check). "
        "'off': no verification (NOT recommended — the binary is executed on every run). "
        "On failure the fetch is rejected; nothing is cached or served.",
    )
    signing_keys: dict[str, str] = Field(
        default_factory=dict,
        description="Operator override for the pinned publisher public keys used to verify "
        "binary SHA256SUMS, keyed by tool ('terraform'/'tofu'/'terragrunt') → ASCII-armored "
        "public key. Empty (default) uses the keys bundled in the image (HashiCorp "
        "34365D9472D7468F, OpenTofu 0C0AF313E5FD9F80, Gruntwork 577774ACA847CC49). Supply a "
        "key here to bridge an upstream key rotation without waiting for a Terrapod release, "
        "or to trust an internal re-signing mirror. Provided keys are propagated to runner "
        "Jobs so runner-side verification honours the same trust set.",
    )


class ModuleInterfaceConfig(BaseModel):
    """Module interface extraction (inputs/outputs from HCL)."""

    enabled: bool = Field(default=True)


class RegistryConfig(BaseModel):
    """Private registry and caching configuration."""

    enabled: bool = Field(default=True)
    cache_only: bool = Field(
        default=False,
        description="Sealed (cache-only) mode for air-gapped deployments. When true, "
        "NO upstream fetch ever happens across the binary cache, provider network "
        "mirror, terragrunt binary, and version resolution: a cache miss returns a "
        "clear, actionable error instead of (and never even attempting) an upstream "
        "request, partial-version resolution (e.g. '1.12') resolves ONLY against "
        "cached entries, and the artifact-retention sweeper skips the binary/provider "
        "caches (evicting an un-refetchable artifact would lose it permanently). "
        "Pre-populate the cache first (bulk-warm admin endpoint / UI) — typically "
        "with cache_only off, pointing at an internal mirror — then seal. Pairs with "
        "the forward proxy/CA as defense-in-depth: the proxy controls HOW upstream "
        "would be reached; cache_only guarantees it ISN'T.",
    )
    signing_key: str = Field(
        default="",
        description="ASCII-armored GPG private key for provider signing. "
        "If set, imported on first use and used to sign all provider SHA256SUMS. "
        "If empty, a key is auto-generated on first provider upload. "
        "Set via TERRAPOD_REGISTRY__SIGNING_KEY env var or K8s Secret.",
    )
    provider_cache: ProviderCacheConfig = Field(default_factory=ProviderCacheConfig)
    binary_cache: BinaryCacheConfig = Field(default_factory=BinaryCacheConfig)
    module_interface: ModuleInterfaceConfig = Field(default_factory=ModuleInterfaceConfig)


class CatalogConfig(BaseModel):
    """Service catalog: no-code self-service provisioning over the module
    registry (#535).

    On by default — it exposes the catalog RBAC axis (opt-in, default `none`,
    so no user gains catalog access until granted), catalog-item and
    provider-template management, and the provision flow. Set `enabled: false`
    to hide the surface entirely (endpoints return 404).
    """

    enabled: bool = Field(default=True)


# --- VCS Configuration ---


class GitHubWebhookConfig(BaseModel):
    """GitHub webhook configuration (optional, for faster feedback)."""

    webhook_secret: str = Field(
        default="",
        description="Webhook secret for HMAC signature validation (optional)",
    )


class GitLabWebhookConfig(BaseModel):
    """GitLab webhook configuration (optional, for faster feedback).

    GitLab does not HMAC-sign the body — it sends the configured secret
    verbatim in the ``X-Gitlab-Token`` header. This global secret is the
    fallback when a VCS connection does not set its own ``webhook_secret``.
    """

    webhook_secret: str = Field(
        default="",
        description="Webhook secret matched against the X-Gitlab-Token header (optional)",
    )


class VCSConfig(BaseModel):
    """VCS integration configuration."""

    enabled: bool = Field(default=True, description="Enable VCS integration")
    poll_interval_seconds: int = Field(
        default=60, description="Polling interval in seconds for VCS changes"
    )
    github: GitHubWebhookConfig = Field(default_factory=GitHubWebhookConfig)
    gitlab: GitLabWebhookConfig = Field(default_factory=GitLabWebhookConfig)
    tmpdir: str = Field(
        default="/var/lib/terrapod/tmp",
        description=(
            "Directory for temporary VCS archive files (raw + stripped tarballs). "
            "Should point at an ephemeral PVC mount in production — multi-hundred-MB "
            "monorepo tarballs land here during streaming download/strip/upload, "
            "and a node tmpfs typically isn't large enough. The Helm chart provisions "
            "this via `api.ephemeralStorage` and mounts it at this path. Falls back "
            "to the system tempdir at runtime if the path doesn't exist (suitable for "
            "tests and local dev)."
        ),
    )
    archive_cache_retention_days: int = Field(
        default=7,
        description=(
            "TTL for cached VCS archive tarballs in object storage. Stripped "
            "tarballs are content-addressed by commit SHA so they're safe to "
            "cache for the lifetime of any in-flight runs that reference them. "
            "Evicted by the artifact-retention sweeper."
        ),
    )
    tmpdir_min_free_bytes: int = Field(
        default=2 * 1024**3,  # 2 GiB
        description=(
            "Minimum free space in `tmpdir` before each VCS download. If free "
            "space drops below this, the cache evicts the oldest orphan temp "
            "tarballs (anything older than 5 minutes that didn't get cleaned "
            "up by its NamedTemporaryFile context — e.g. a previous pod crash) "
            "until we hit the threshold or run out of candidates. Stops the "
            "ephemeral PVC from filling up and breaking subsequent polls."
        ),
    )


# --- Drift Detection Configuration ---


class DriftDetectionConfig(BaseModel):
    """Drift detection configuration.

    When enabled, a periodic scheduler task checks all drift-enabled workspaces
    and creates plan-only runs to detect infrastructure drift.
    """

    enabled: bool = Field(default=True, description="Enable drift detection")
    poll_interval_seconds: int = Field(
        default=300,
        description="How often the scheduler checks for workspaces due for drift detection",
    )
    min_workspace_interval_seconds: int = Field(
        default=3600,
        description="Minimum per-workspace drift check interval (floor, 1 hour)",
    )


# --- Slack Integration ---


class SlackConfig(BaseModel):
    """Interactive Slack integration (#556).

    Phase 1 wires the connection: an outbound **Socket Mode** WebSocket to Slack
    (no public/inbound URL required, matching the restricted-network execution
    model) plus a connectivity check. Later phases add action buttons, the
    `/terrapod` slash command, and account-linked, RBAC-checked approve/discard.

    The three tokens are **secrets** and arrive via env (secretKeyRef), never the
    ConfigMap — `TERRAPOD_SLACK__BOT_TOKEN` / `__APP_TOKEN` / `__SIGNING_SECRET`.
    Full operator setup (incl. the Slack-admin ↔ operator handoff): see
    docs/slack-integration.md.
    """

    enabled: bool = Field(default=False, description="Enable the Slack integration")
    socket_mode: bool = Field(
        default=True,
        description=(
            "Use Socket Mode — an outbound WebSocket, no public URL. When false, "
            "Slack reaches the Request-URL endpoint via the public webhook ingress."
        ),
    )
    # --- secrets: delivered via secretKeyRef → env, never rendered to the ConfigMap ---
    bot_token: str = Field(default="", description="Bot User OAuth Token (xoxb-…)")
    app_token: str = Field(
        default="", description="App-Level Token with connections:write (xapp-…), for Socket Mode"
    )
    signing_secret: str = Field(default="", description="Slack app signing secret")


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


class AgentPoolsConfig(BaseModel):
    """Agent pool join-token defaults and listener-certificate lifetime.

    Tighter defaults reduce the blast radius of a leaked join token; looser
    defaults reduce operational noise from re-issuance on routine pod
    restarts that miss the renewal window. Listeners renew their cert at
    50% of `listener_cert_ttl_seconds` (plus a small per-pod splay), so
    that value also controls how often the API reissues a fresh cert.
    """

    listener_cert_ttl_seconds: int = Field(
        default=3600,
        description=(
            "Listener X.509 certificate lifetime in seconds (default 1h). "
            "Listeners renew at 50% of TTL plus a per-pod splay, so this "
            "drives both how fast a compromised cert ages out and how often "
            "the API issues fresh ones. For local Tilt, override to ~300s "
            "in values-local.yaml so a renewal cycle finishes in minutes."
        ),
    )
    default_join_token_max_uses: int | None = Field(
        default=2,
        description=(
            "Default max_uses on newly-created join tokens. 2 tolerates a "
            "single bootstrap-race retry across multi-replica listener "
            "deployments without making the token reusable indefinitely. "
            "Per-token override is still accepted via the API."
        ),
    )
    default_join_token_ttl_seconds: int | None = Field(
        default=3600,
        description=(
            "Default lifetime for newly-created join tokens, in seconds. "
            "Applied as expires_at = now() + this. Default 1h. Set to null "
            "to default to no expiry (per-token override still accepted)."
        ),
    )


class RateLimitConfig(BaseModel):
    """API rate limiting configuration.

    Each limit is per client IP per 60-second sliding window. Set a limit
    to 0 to disable that tier (unlimited).

    The runner tier is separate and defaults to unlimited — runners are
    trusted service-to-service callers (HMAC-verified inline) that burst
    on tofu init / apply artifact uploads. Raise `runner_requests_per_minute`
    above 0 only if you need a hard cap.
    """

    enabled: bool = Field(default=True, description="Enable rate limiting")
    requests_per_minute: int = Field(
        default=100,
        description="Max requests per minute for unauthenticated API endpoints. 0 = unlimited.",
    )
    authenticated_requests_per_minute: int = Field(
        default=1000,
        description=(
            "Max requests per minute for requests carrying an Authorization "
            "header or session cookie (non-runner). 0 = unlimited."
        ),
    )
    runner_requests_per_minute: int = Field(
        default=0,
        description=(
            "Max requests per minute for verified runner-token requests. "
            "Defaults to 0 (unlimited) because runners routinely burst on "
            "tofu init / apply artifact uploads and throttling them causes "
            "plan failures. Raise if you need a hard cap."
        ),
    )
    auth_requests_per_minute: int = Field(
        default=10,
        description="Max requests per minute for auth endpoints (login). 0 = unlimited.",
    )


# --- Metrics Configuration ---


class ArtifactRetentionConfig(BaseModel):
    """Artifact retention and cleanup configuration.

    When enabled, a periodic scheduler task cleans up old artifacts from
    object storage and the database.  Zero on any per-category setting
    disables that category's cleanup.
    """

    enabled: bool = Field(default=True, description="Enable artifact retention cleanup")
    poll_interval_seconds: int = Field(
        default=86400, description="How often the cleanup task runs (default: daily)"
    )
    batch_size: int = Field(
        default=100, description="Max items to process per cleanup category per cycle"
    )
    state_versions_keep: int = Field(
        default=20,
        description="Number of state versions to keep per workspace (0 = disabled)",
    )
    run_artifacts_retention_days: int = Field(
        default=90,
        description="Days to keep run logs + plans for terminal runs (0 = disabled)",
    )
    config_versions_retention_days: int = Field(
        default=90,
        description="Days to keep config version tarballs (0 = disabled)",
    )
    config_versions_keep: int = Field(
        default=50,
        description=(
            "Number of configuration versions to keep per workspace beyond "
            "what is referenced by an active run (0 = disabled). Mirrors "
            "the registry-module retain-by-count pattern. Runs that "
            "reference an older CV continue to render — the CV row "
            "stays via the SET NULL FK — but the tarball is deleted "
            "and the CV is no longer downloadable."
        ),
    )
    provider_cache_retention_days: int = Field(
        default=30,
        description="Days since last access before cached provider binaries are eligible for cleanup (0 = disabled)",
    )
    binary_cache_retention_days: int = Field(
        default=30,
        description="Days since last access before cached CLI binaries are eligible for cleanup (0 = disabled)",
    )
    module_overrides_retention_days: int = Field(
        default=14,
        description="Days to keep module override tarballs for terminal runs (0 = disabled)",
    )


class RunnerArtifactsConfig(BaseModel):
    """API-side limits for runner artifact uploads.

    Distinct from `RunnerConfig` (which lives in `runners.yaml` and
    configures the listener / runner Jobs). These knobs control the
    API endpoints that runners upload artifacts to.
    """

    plan_artifacts_max_bytes: int = Field(
        default=256 * 1024 * 1024,
        ge=10240,
        description=(
            "Maximum size (bytes) of the plan-phase workspace-diff "
            "tarball that the runner uploads at end of plan and "
            "downloads at start of apply. Restores plan-time generated "
            "artifacts (data.archive_file outputs, etc.) that would "
            "otherwise be missing across the plan→apply Job boundary. "
            "Uploads exceeding this size are rejected with 413; the "
            "runner logs the rejection, apply proceeds without the "
            "restore, and any resource that depended on a plan-time "
            "generated file will fail at apply with a clear error. "
            "Default 256 MiB. Minimum 10240 bytes — the size of an "
            "empty tar with Python's default 20-block padding, which "
            "every run uploads even when plan produced no new files."
        ),
    )


class MetricsConfig(BaseModel):
    """Prometheus metrics configuration."""

    enabled: bool = Field(
        default=True, description="Expose /metrics endpoint and instrument requests"
    )


class EncryptionConfig(BaseModel):
    """Optional application-layer encryption at rest (#553).

    OFF by default. For deployments WITHOUT a CSP at-rest encryption switch
    (bare-metal / on-prem / niche cloud / air-gapped) this is the path to
    encryption at rest; if your CSP already encrypts at rest (RDS/S3/Azure/GCS),
    prefer that and treat this as belt-and-braces. Envelope encryption with a
    pluggable KEK provider — see docs/encryption-at-rest.md.

    Secrets (the static master key, the Vault token) are NOT here — they come
    from env (`TERRAPOD_ENCRYPTION__STATIC_KEY` / `__VAULT_TOKEN`) via a K8s
    Secret, never a ConfigMap.
    """

    enabled: bool = Field(default=False, description="Enable app-layer encryption at rest")
    provider: str = Field(
        default="static",
        description="KEK provider: static | vault_transit | awskms",
    )
    # Vault Transit (CSP-agnostic; on-prem / air-gapped)
    vault_address: str = Field(default="", description="Vault address, e.g. https://vault:8200")
    vault_mount: str = Field(default="transit", description="Vault transit mount path")
    vault_key_name: str = Field(default="terrapod", description="Vault transit key name")
    vault_namespace: str = Field(default="", description="Vault namespace (Enterprise; optional)")
    # AWS KMS (cloud belt-and-braces)
    aws_kms_key_id: str = Field(default="", description="AWS KMS key ARN or id")
    aws_kms_region: str = Field(default="", description="AWS region for KMS (optional)")

    @field_validator("provider")
    @classmethod
    def _valid_provider(cls, v: str) -> str:
        allowed = {"static", "vault_transit", "awskms"}
        if v not in allowed:
            raise ValueError(f"encryption.provider must be one of {sorted(allowed)}")
        return v


class BackupConfig(BaseModel):
    """Logical PostgreSQL backup settings (consumed by terrapod.cli.backup).

    The backup CronJob (off by default in the chart) runs ``pg_dump`` and
    streams the dump to the configured object store under ``prefix``. This is a
    logical dump — RPO is the dump interval, not point-in-time — and is the
    baseline floor, not a replacement for RDS snapshots / WAL-G / pgBackRest.
    See docs/disaster-recovery.md.
    """

    prefix: str = Field(
        default="backups/",
        description="Object-store key prefix backups are written under",
    )
    retention_keep: int = Field(
        default=0,
        ge=0,
        description="Number of most-recent backups to keep (0 = keep all)",
    )
    retention_days: int = Field(
        default=0,
        ge=0,
        description="Delete backups older than this many days (0 = disabled)",
    )


# --- AI Plan Summary Configuration ---


class AISummaryAuthConfig(BaseModel):
    """Auth configuration for the AI summariser.

    LiteLLM picks the right auth path per provider automatically: bearer
    for OpenAI/Anthropic/Gemini/Azure/vLLM (read from the relevant env
    var or this config), AWS credential chain for Bedrock (boto3 inherits
    the pod's IRSA creds and optionally hops through sts:AssumeRole).
    No mode switch — the model string's prefix (``bedrock/``,
    ``openai/``, ``anthropic/``, ``gemini/``, etc.) is what selects the
    underlying auth path.
    """

    api_key: str = Field(
        default="",
        description=(
            "Bearer API key for non-AWS providers (OpenAI, Anthropic "
            "direct, Gemini, Azure OpenAI, vLLM gateway, etc.). Inject "
            "from a K8s Secret via env var (TERRAPOD_AI_SUMMARY__AUTH__API_KEY) "
            "rather than committing to values.yaml. Ignored when the "
            "model string targets a provider LiteLLM authenticates "
            "another way (e.g. ``bedrock/...`` uses the AWS credential "
            "chain instead)."
        ),
    )
    aws_region: str = Field(
        default="us-east-1",
        description=(
            "AWS region for Bedrock invocation. Used only when the "
            "model string starts with ``bedrock/``. Passed through to "
            "LiteLLM as ``aws_region_name``."
        ),
    )
    aws_role_arn: str = Field(
        default="",
        description=(
            "Cross-account IAM role to assume for Bedrock access. Empty "
            "means use the pod's ambient credentials (IRSA / env-var / "
            "shared credentials file) directly. When set, LiteLLM calls "
            "sts:AssumeRole and caches the temporary credentials, "
            "refreshing them before expiry. Passed to LiteLLM as "
            "``aws_role_name``."
        ),
    )
    aws_session_name: str = Field(
        default="terrapod-ai-summary",
        description=(
            "STS AssumeRole session name (visible in CloudTrail). Only "
            "used when ``aws_role_arn`` is set."
        ),
    )
    aws_external_id: str = Field(
        default="",
        description=(
            "Optional ExternalId for the AssumeRole call. Set when the "
            "destination role's trust policy requires it."
        ),
    )


class AISummaryContextConfig(BaseModel):
    """Layered context injected into the model prompt.

    Layers (rendered top-to-bottom in the request):
      1. In-code skill prompt — owns the JSON output contract, NOT
         operator-overridable. Lives in `summariser_prompt.py`.
      2. `prompt_prefix` (this block) — free-text prepended.
      3. `fleet_context` — facts about this Terrapod deployment.
      4. `prompt_suffix` — free-text appended.
      5. Per-workspace `ai_summary_context` column — workspace-specific.

    Layers 2 and 4 are escape hatches for tone/emphasis tweaks; do not
    use them to change the output schema (it is wired to the DB schema
    and the SSE/UI contract).
    """

    prompt_prefix: str = Field(
        default="",
        description=(
            "Free-text prepended before the skill prompt. Use for tone "
            "or emphasis tweaks ('be terse'). Do not change the output "
            "schema instructions here — that breaks the UI contract."
        ),
    )
    fleet_context: str = Field(
        default="",
        description=(
            "Static deployment-wide facts about the infrastructure this "
            "Terrapod instance manages (what runs here, naming "
            "conventions, provider/action pairs to flag, etc.). Rendered "
            "below the skill prompt and above the per-workspace context."
        ),
    )
    prompt_suffix: str = Field(
        default="",
        description=(
            "Free-text appended after fleet_context and before the "
            "per-workspace context. Same caveats as prompt_prefix."
        ),
    )


class AISummaryConfig(BaseModel):
    """AI plan-summary configuration (#401).

    When enabled, every successful plan triggers an asynchronous call to
    an OpenAI-compatible Chat Completions endpoint. The model is given
    the structured plan JSON plus optional code context and returns a
    structured summary (human description + risk assessment). The
    summary is stored in the plan_summaries table, surfaced in the run
    UI, and (for VCS-driven runs) edited into the PR/MR comment in place.

    All deployments default to disabled — enabling requires both an
    auth secret AND explicit endpoint + model config.
    """

    enabled: bool = Field(
        default=False,
        description="Master switch. Off by default — no calls made.",
    )
    model: str = Field(
        default="",
        description=(
            "LiteLLM model string. The prefix selects the provider and "
            "auth path; the suffix names the model. Examples: "
            "``bedrock/anthropic.claude-opus-4-8`` (AWS Bedrock + IAM), "
            "``openai/gpt-5`` (OpenAI direct + api_key), "
            "``anthropic/claude-opus-4-8`` (Anthropic direct + api_key), "
            "``gemini/gemini-2.5-pro`` (Google AI Studio + api_key), "
            "``azure/<deployment-name>`` (Azure OpenAI). For self-hosted "
            "OpenAI-compat endpoints (vLLM, LiteLLM proxy), use "
            "``openai/<model>`` with ``api_base`` set below."
        ),
    )
    api_base: str = Field(
        default="",
        description=(
            "Override the upstream base URL. Only needed for "
            "self-hosted OpenAI-compat endpoints (vLLM, a deployed "
            "LiteLLM proxy, etc.) or to pin a specific Azure OpenAI "
            "instance. Leave empty for vendor providers — LiteLLM "
            "knows the default URLs."
        ),
    )
    max_output_tokens: int = Field(
        default=16384,
        description=(
            "Upper bound on model response tokens. Must accommodate the "
            "full JSON object: ~600 words of description + a risk-factor "
            "list whose size scales with the plan. This is a CAP, not a "
            "target — the model only emits what it needs and stops on a "
            "natural stop token; only oversized plans approach it. 1024 "
            "was undersized for medium plans and caused finish_reason="
            "'length' truncation; 16384 leaves comfortable headroom for "
            "very large plans (100+ resource changes with detailed risk "
            "listings) while staying well under Opus's 32K output limit."
        ),
    )
    request_timeout_seconds: int = Field(
        default=60,
        description="HTTP timeout for a single summariser call.",
    )
    daily_token_budget: int = Field(
        default=0,
        description=(
            "Cap on output tokens spent per UTC day across all summaries. "
            "0 = unlimited. Counter is maintained in Redis; calls past "
            "the cap are skipped (with a log entry, no run failure)."
        ),
    )
    code_context_max_bytes: int = Field(
        default=200_000,
        description=(
            "Max bytes of .tf source attached as context alongside the "
            "plan JSON. The runner already uploads the config tarball; "
            "summariser reads it from object storage and concatenates "
            "the .tf files (truncated to this cap). 0 disables code "
            "context entirely (plan JSON only — terser, cheaper, less "
            "accurate)."
        ),
    )
    plan_json_max_bytes: int = Field(
        default=600_000,
        description=(
            "Max bytes of plan JSON attached to the model request. This is a "
            "last-resort ceiling for the model's context window, not routine "
            "trimming: the cap only engages on plans with ~1000+ resource "
            "changes (a cleaned change is a few hundred bytes); everything "
            "smaller is sent whole and untouched. When it does engage, the "
            "reduction is STRUCTURAL (`_fit_plan_json`) — every change keeps "
            "its address and `change.actions`, so a destroy can never be "
            "hidden; only per-resource attribute detail is trimmed. Defends "
            "against pathological monorepos producing tens-of-MB plan files "
            "that would otherwise overflow the context window and hard-fail "
            "the summary."
        ),
    )
    code_diff_max_bytes: int = Field(
        default=100_000,
        description=(
            "Max bytes of CODE_DIFF (unified diff of *.tf / *.tfvars "
            "between this run's config version and the previously-applied "
            "config version) attached to the request. The diff is computed "
            "via `git diff --no-index` between the two tarballs. 0 disables "
            "CODE_DIFF entirely. Diffs are usually tiny; this cap is a "
            "safety net for monorepo-scale refactors."
        ),
    )
    followup_max_messages_per_run: int = Field(
        default=20,
        description=(
            "Cap on user-posted follow-up messages per run (#463). "
            "The UI disables the chat input once a run reaches this "
            "many user turns. 0 disables the chat feature entirely "
            "(initial summary still fires). Counts user rows in "
            "`plan_summary_messages` for the run — assistant rows "
            "don't count against the cap."
        ),
    )
    followup_max_output_tokens: int = Field(
        default=2048,
        description=(
            "Upper bound on follow-up reply tokens (#463). Smaller "
            "than `max_output_tokens` because follow-ups are "
            "conversational text-in / text-out, not a full structured "
            "re-summary. Bumped only when operators report routinely "
            "hitting `finish_reason=length` on detailed answers."
        ),
    )
    auth: AISummaryAuthConfig = Field(default_factory=AISummaryAuthConfig)
    context: AISummaryContextConfig = Field(default_factory=AISummaryContextConfig)


class DatabaseConfig(BaseModel):
    """SQLAlchemy connection pool settings.

    Tunable for RDS Proxy, pgBouncer, and high-availability PostgreSQL
    deployments where connection lifetime and idle management matter.
    """

    pool_size: int = Field(
        default=10,
        description="Number of persistent connections in the pool",
    )
    max_overflow: int = Field(
        default=20,
        description="Max additional connections beyond pool_size",
    )
    pool_pre_ping: bool = Field(
        default=True,
        description="Test connections with SELECT 1 before checkout (handles stale connections)",
    )
    pool_recycle: int = Field(
        default=1800,
        description="Recycle connections after N seconds (set below proxy max_connection_lifetime)",
    )
    pool_timeout: int = Field(
        default=30,
        description="Seconds to wait for a connection from the pool before raising an error",
    )
    connect_timeout: int = Field(
        default=10,
        description="Seconds to wait for initial TCP connection to the database",
    )
    command_timeout: int = Field(
        default=30,
        description="Seconds to wait for a query to complete",
    )
    # --- Authentication mode (#573) ---
    auth_mode: Literal["password", "aws_iam", "gcp_iam", "azure_ad"] = Field(
        default="password",
        description=(
            "Database authentication. 'password' (default) uses the static "
            "password in database_url — the existing, fully-supported behaviour. "
            "The cloud-IAM modes mint a short-lived token per connection under the "
            "API pod's workload identity — no static DB password, TLS required: "
            "'aws_iam' (AWS RDS IAM, IRSA), 'gcp_iam' (GCP Cloud SQL IAM, WIF), "
            "'azure_ad' (Azure Database for PostgreSQL Entra auth, Azure WI)."
        ),
    )
    aws_iam_region: str = Field(
        default="",
        description=(
            "AWS region used to sign RDS IAM auth tokens. Empty = botocore default "
            "resolution (AWS_REGION / AWS_DEFAULT_REGION, set by IRSA). Only used "
            "when auth_mode='aws_iam'."
        ),
    )
    ssl_mode: str = Field(
        default="",
        description=(
            "TLS mode for cloud-IAM auth: 'require' (encrypt, no cert check — "
            "the default when empty), 'verify-ca' (verify the server cert chain) "
            "or 'verify-full' (verify chain + hostname). 'verify-ca'/'verify-full' "
            "require ssl_root_cert (or a system-trusted CA). Cloud IAM auth always "
            "uses at least 'require'. Ignored when auth_mode='password'."
        ),
    )
    ssl_root_cert: str = Field(
        default="",
        description=(
            "Path to a CA bundle (PEM) used to verify the database server "
            "certificate when ssl_mode is 'verify-ca'/'verify-full' (e.g. the AWS "
            "RDS global-bundle.pem or the Cloud SQL server CA, mounted via "
            "api.extraVolumes). Empty = the system trust store. Only used by the "
            "cloud-IAM auth modes."
        ),
    )


class RedisConfig(BaseModel):
    """Redis/Valkey authentication settings (#579).

    The connection URL itself stays in the top-level ``redis_url``; this block
    only carries the cloud-IAM auth knobs. Default ``auth_mode='password'`` uses
    the static auth string in ``redis_url`` and is fully supported.
    """

    auth_mode: Literal["password", "aws_iam", "gcp_iam", "azure_ad"] = Field(
        default="password",
        description=(
            "Redis authentication. 'password' (default) uses the static auth "
            "string in redis_url. The cloud-IAM modes mint a short-lived token "
            "per connection under the API pod's workload identity — no static "
            "Redis secret, TLS (rediss://) required: 'aws_iam' (AWS ElastiCache "
            "IAM, IRSA), 'gcp_iam' (GCP Memorystore IAM, WIF), 'azure_ad' (Azure "
            "Cache for Redis Entra auth, Azure WI)."
        ),
    )
    username: str = Field(
        default="",
        description=(
            "Redis ACL username for IAM auth — the ElastiCache User (AWS), the "
            "IAM user (GCP), or the Entra principal object id (Azure). Required "
            "for the cloud-IAM modes; ignored for 'password'."
        ),
    )
    aws_iam_region: str = Field(
        default="",
        description=(
            "AWS region used to sign ElastiCache IAM tokens. Empty = botocore "
            "default resolution. Only used when auth_mode='aws_iam'."
        ),
    )
    aws_cache_name: str = Field(
        default="",
        description=(
            "ElastiCache cache identifier used for SigV4 signing — the "
            "replication-group id or serverless cache name (NOT the endpoint "
            "host). Required when auth_mode='aws_iam'."
        ),
    )

    @model_validator(mode="after")
    def _require_iam_fields(self) -> "RedisConfig":
        """Fail fast on misconfigured IAM auth instead of an opaque connect error."""
        if self.auth_mode in ("aws_iam", "gcp_iam", "azure_ad") and not self.username:
            raise ValueError(f"redis.username is required for auth_mode={self.auth_mode!r}")
        if self.auth_mode == "aws_iam" and not self.aws_cache_name:
            raise ValueError("redis.aws_cache_name is required for auth_mode='aws_iam'")
        return self


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
    public_webhook_url: str = Field(
        default="",
        description=(
            "Base URL Terrapod hands to EXTERNAL services that call back into us "
            "from the public internet (VCS webhook URL displayed in the connection "
            "edit screen; run-task callback URL in the dispatched webhook payload). "
            "Independent of `external_url` so deployments can put the management "
            "plane on a private/restricted ingress while exposing only the webhook "
            "surface publicly. When empty, callers fall back to `external_url`."
        ),
    )

    # Token signing — dedicated secret for stateless HMAC tokens (runner
    # tokens + run-task callback tokens). When empty, the signing key falls
    # back to sha256(database_url) for backward compatibility (no in-flight
    # token is invalidated by upgrading). Set this (Helm: api.tokenSigningKey
    # / env TERRAPOD_TOKEN_SIGNING_KEY) to decouple token-forgery resistance
    # from database credentials. See auth/token_signing.py.
    token_signing_key: str = Field(
        default="",
        description="Dedicated HMAC secret for runner + run-task tokens "
        "(falls back to sha256(database_url) when empty).",
    )

    # Database
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://terrapod:terrapod@localhost:5432/terrapod",
        description="PostgreSQL connection URL",
    )
    database: "DatabaseConfig" = Field(default_factory=lambda: DatabaseConfig())

    # Redis
    redis_url: RedisDsn = Field(
        default="redis://localhost:6379",
        description="Redis connection URL",
    )
    redis: "RedisConfig" = Field(default_factory=lambda: RedisConfig())

    # Storage
    storage: StorageConfig = Field(default_factory=StorageConfig)

    # Authentication
    auth: AuthConfig = Field(default_factory=AuthConfig)

    # Audit
    audit: AuditConfig = Field(default_factory=AuditConfig)

    # Registry
    registry: RegistryConfig = Field(default_factory=RegistryConfig)

    # Service Catalog
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)

    # VCS
    vcs: VCSConfig = Field(default_factory=VCSConfig)

    # Notifications
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)

    # Drift Detection
    drift_detection: DriftDetectionConfig = Field(default_factory=DriftDetectionConfig)

    # Slack integration (#556)
    slack: SlackConfig = Field(default_factory=SlackConfig)

    # CORS
    cors: CORSConfig = Field(default_factory=CORSConfig)

    # Agent Pools
    agent_pools: AgentPoolsConfig = Field(default_factory=AgentPoolsConfig)

    # Rate Limiting
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)

    # Metrics
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)

    # Artifact Retention
    artifact_retention: ArtifactRetentionConfig = Field(default_factory=ArtifactRetentionConfig)

    # Logical PostgreSQL backup (terrapod.cli.backup; CronJob off by default)
    backup: BackupConfig = Field(default_factory=BackupConfig)

    # Optional app-layer encryption at rest (#553; off by default)
    encryption: EncryptionConfig = Field(default_factory=EncryptionConfig)

    # Runner artifact upload limits (API-side enforcement)
    runner_artifacts: RunnerArtifactsConfig = Field(default_factory=RunnerArtifactsConfig)

    # AI Plan Summary (#401)
    ai_summary: AISummaryConfig = Field(default_factory=AISummaryConfig)

    # Workspace defaults
    default_execution_backend: str = Field(
        default="tofu",
        description="Default execution backend for new workspaces (tofu or terraform)",
    )
    default_terraform_version: str = Field(
        default="1.12",
        description="Default terraform/tofu version for new workspaces",
    )

    # API
    # `api_prefix` is the TFE V2 CLI-contract surface (terraform / tofu /
    # tfci / go-tfe). `terrapod_prefix` is the Terrapod-native surface
    # (auth, admin, registry mgmt, etc.). The OAuth/SAML callback lives
    # on the Terrapod-native surface — see auth.py — so its URL is built
    # from `terrapod_prefix`, not `api_prefix`.
    api_prefix: str = Field(default="/api/v2")
    terrapod_prefix: str = Field(default="/api/terrapod/v1")

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
