"""
Key path helpers for object storage.

Provides consistent key naming for all stored artifacts.
All keys are relative to the storage backend's configured prefix.
"""


def state_index_key() -> str:
    """Key for the human-readable state index (break-glass DR recovery)."""
    return "state/index.yaml"


#: Prefix for workspace delete markers (#1253). Deliberately a FLAT prefix
#: rather than a marker nested inside each workspace's `state/{id}/`:
#:
#:   * the `state` blob class is `encrypted_at_rest`, and replication declines
#:     that whole class when app-layer encryption is on — a nested marker would
#:     inherit that and leave an encrypted deployment's standby with no
#:     undelete index at all;
#:   * markers are tiny, so they can be `copy`-replicated even where state
#:     itself is `verify`-only;
#:   * listing the undelete set is one flat listing rather than a walk over
#:     every workspace prefix, and the restore path can copy a workspace's
#:     state prefix wholesale without dragging a marker into the workspace it
#:     just restored.
DELETED_MARKER_PREFIX = "state/deleted/"


def deleted_workspace_marker_key(workspace_id: str) -> str:
    """Key for a deleted workspace's marker."""
    return f"{DELETED_MARKER_PREFIX}{workspace_id}.json"


def state_key(workspace_id: str, version_id: str) -> str:
    """Key for a workspace state version."""
    return f"state/{workspace_id}/{version_id}.tfstate"


def state_backup_key(workspace_id: str, version_id: str) -> str:
    """Key for a backup of a workspace state version."""
    return f"state/{workspace_id}/{version_id}.backup.tfstate"


def plan_log_key(workspace_id: str, run_id: str) -> str:
    """Key for a run's plan log output."""
    return f"logs/{workspace_id}/plans/{run_id}.log"


def apply_log_key(workspace_id: str, run_id: str) -> str:
    """Key for a run's apply log output."""
    return f"logs/{workspace_id}/applies/{run_id}.log"


def plan_output_key(workspace_id: str, run_id: str) -> str:
    """Key for a run's binary plan output (tfplan file)."""
    return f"plans/{workspace_id}/{run_id}.tfplan"


def plan_json_output_key(workspace_id: str, run_id: str) -> str:
    """Key for a run's structured JSON plan output (`tofu show -json tfplan`)."""
    return f"plans/{workspace_id}/{run_id}.json-output"


def cost_estimate_key(workspace_id: str, run_id: str) -> str:
    """Key for a run's cost estimate (`cost_estimate.json`, #871).

    The native cost estimate of the plan's monthly cost delta,
    produced by the runner from the plan JSON and uploaded as a run artifact —
    the cost analogue of `plan_json_output_key`.
    """
    return f"plans/{workspace_id}/{run_id}.cost-estimate.json"


def lock_file_key(workspace_id: str, run_id: str) -> str:
    """Key for the `.terraform.lock.hcl` produced by the plan-phase init.

    Carried to the apply phase so it inits with the same provider versions
    instead of re-resolving the version constraint (which could pick up a
    newer matching version published in the plan→apply window and cause
    `apply tfplan` to fail with a recorded-plan mismatch). See #306.
    """
    return f"plans/{workspace_id}/{run_id}.terraform.lock.hcl"


def plan_artifacts_key(workspace_id: str, run_id: str) -> str:
    """Key for the plan-phase workspace diff tarball.

    The runner snapshots the workspace file tree after init and again
    after plan, and uploads the set difference (paths that exist after
    plan but didn't after init) as a plain `tar`. The apply phase
    downloads + extracts this over its initialised workspace, restoring
    any plan-time generated artifacts (e.g. `data.archive_file` zips)
    that would otherwise be missing because each phase runs in a
    fresh K8s Job. tfplan + lock file are uploaded via their own
    endpoints and are excluded from this tarball to avoid duplication.
    """
    return f"plans/{workspace_id}/{run_id}.plan-artifacts.tar"


def config_version_key(workspace_id: str, config_version_id: str) -> str:
    """Key for a configuration version archive."""
    return f"config/{workspace_id}/{config_version_id}.tar.gz"


def run_tfvars_key(workspace_id: str, run_id: str) -> str:
    """Key for a generated .tfvars file for a run."""
    return f"runs/{workspace_id}/{run_id}.auto.tfvars"


def policy_set_key(policy_set_id: str, version_id: str) -> str:
    """Key for a policy set bundle."""
    return f"policies/{policy_set_id}/{version_id}.tar.gz"


# --- Module Registry ---


def module_tarball_key(namespace: str, name: str, provider: str, version: str) -> str:
    """Key for a module version tarball."""
    return f"registry/modules/{namespace}/{name}/{provider}/{version}.tar.gz"


# --- Provider Registry ---


def provider_binary_key(namespace: str, name: str, version: str, os_: str, arch: str) -> str:
    """Key for a provider binary zip."""
    return f"registry/providers/{namespace}/{name}/{version}/{name}_{version}_{os_}_{arch}.zip"


def provider_shasums_key(namespace: str, name: str, version: str) -> str:
    """Key for a provider version's SHA256SUMS file."""
    return f"registry/providers/{namespace}/{name}/{version}/SHA256SUMS"


def provider_shasums_sig_key(namespace: str, name: str, version: str) -> str:
    """Key for a provider version's SHA256SUMS.sig file."""
    return f"registry/providers/{namespace}/{name}/{version}/SHA256SUMS.sig"


# --- Provider Cache ---


def provider_cache_key(
    hostname: str, namespace: str, type_: str, version: str, filename: str
) -> str:
    """Key for a cached upstream provider binary."""
    return f"cache/providers/{hostname}/{namespace}/{type_}/{version}/{filename}"


# --- Binary Cache ---


def binary_cache_key(tool: str, version: str, os_: str, arch: str) -> str:
    """Key for a cached terraform/tofu CLI binary."""
    return f"cache/binaries/{tool}/{version}/{os_}_{arch}"


def binary_cache_sums_key(tool: str, version: str) -> str:
    """Key for the cached publisher SHA256SUMS manifest (per tool+version).

    Persisted at cache time (#607) so runners can verify the executable
    against the publisher's signed manifest without reaching upstream.
    """
    return f"cache/binaries/{tool}/{version}/SHA256SUMS"


def binary_cache_sums_sig_key(tool: str, version: str) -> str:
    """Key for the cached detached GPG signature over the SHA256SUMS manifest."""
    return f"cache/binaries/{tool}/{version}/SHA256SUMS.sig"


def cost_pricesheet_db_key() -> str:
    """Key for the cached pricesheet **SQLite index** (#1034).

    The multi-region sheet (~260k products) is streamed once into a SQLite index
    at cache refresh and stored here; both the API and runner download this file
    and query it off disk (bounded memory) instead of parsing the whole sheet.
    A distinct key from the raw-sheet key above so the two never get confused.
    """
    return "cache/cost/prices.sqlite"


# --- Platform Provider Cache ---


def platform_provider_binary_key(version: str, os_: str, arch: str) -> str:
    """Key for a cached Terrapod platform provider binary."""
    return (
        f"cache/provider/terrapod/{version}/terraform-provider-terrapod_{version}_{os_}_{arch}.zip"
    )


def platform_provider_shasums_key(version: str) -> str:
    """Key for a cached Terrapod platform provider SHA256SUMS."""
    return f"cache/provider/terrapod/{version}/terraform-provider-terrapod_{version}_SHA256SUMS"


def platform_provider_shasums_sig_key(version: str) -> str:
    """Key for a cached Terrapod platform provider SHA256SUMS.sig."""
    return f"cache/provider/terrapod/{version}/terraform-provider-terrapod_{version}_SHA256SUMS.sig"


# --- Module Override (Impact Analysis) ---


def module_override_key(commit_sha: str, namespace: str, name: str, provider: str) -> str:
    """Key for a module override tarball (keyed by commit SHA for reuse across runs)."""
    return f"module_overrides/{commit_sha}/{namespace}/{name}/{provider}.tar.gz"


# --- VCS Archive Cache ---


def vcs_archive_key(
    connection_id: str, owner: str, repo: str, sha: str, paths_hash: str = "full"
) -> str:
    """Key for a cached VCS archive tarball.

    Scoped by `connection_id` so two VCS connections that happen to point
    at the same repo (e.g. one Terrapod app + one personal app for testing)
    can't see each other's cache entries. Content is content-addressed by
    commit SHA + a hash of the path-narrowing set — same SHA + same paths
    means byte-identical tarball, safe to share across workspaces using
    the same connection.

    `paths_hash` defaults to `"full"` for the legacy whole-repo case.
    """
    return f"vcs_archives/{connection_id}/{owner}/{repo}/{sha}-{paths_hash}.tar.gz"
