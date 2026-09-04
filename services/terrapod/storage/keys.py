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


def collection_tarball_key(namespace: str, name: str, version: str) -> str:
    """Where a published Ansible collection's artifact lives (#1482).

    Under `registry/` rather than `cache/`, because it is owned content rather
    than a copy of something upstream — the retention sweep only walks the cache
    prefixes, and a published collection must never be reaped.
    """
    return f"registry/collections/{namespace}/{name}/{version}.tar.gz"


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


# --- Package Cache (language registries) ---


def package_cache_key(ecosystem: str, name: str, filename: str) -> str:
    """Key for a cached artifact from a language package registry (#1417).

    Keyed by filename rather than by version, because a single version routinely
    has many files — an sdist and a wheel per platform and Python ABI — and they
    are not interchangeable.

    The name is already ecosystem-normalised by the caller. npm scopes contain a
    `/` (`@scope/pkg`), which is left intact: object stores treat it as a path
    separator, which is exactly the grouping we would otherwise have to invent.
    """
    return f"cache/packages/{ecosystem}/{name}/{filename}"


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


# ── OCI Distribution registry (#1408) ──────────────────────────────────────
#
# Blobs and manifests are **content-addressed and global**, not nested under a
# repository. That is not a convenience — it is what the spec's cross-repository
# blob mount depends on, and it means two images sharing a base layer share the
# stored bytes rather than duplicating hundreds of MB per repository.
#
# Which repositories may *serve* a given blob is therefore a database question,
# not a storage-layout one: the spec scopes blob reads per repository, so a link
# table decides access, and the same table is what a future reference-walk GC
# has to consult. Encoding that in the key path instead would make dedupe
# impossible and mount unimplementable.
#
# The digest's algorithm becomes a path segment (`sha256/<hex>`) so blobs shard
# rather than landing in one flat prefix — listing a prefix is a paged operation
# on S3, Azure and GCS, and free on the filesystem, so there is no cost.


def oci_blob_key(digest_segment: str) -> str:
    """Key for a content-addressed blob.

    ``digest_segment`` comes from :attr:`~terrapod.services.oci.names.Digest.storage_segment`
    — already validated, so this never sees an unchecked string.
    """
    return f"oci/blobs/{digest_segment}"


def oci_manifest_key(digest_segment: str) -> str:
    """Key for a content-addressed manifest.

    Stored separately from blobs despite also being content-addressed: manifests
    are small, read on every pull, and are what a GC reference walk starts from,
    so keeping them in their own prefix makes that walk a bounded listing rather
    than a scan over every layer in the registry.
    """
    return f"oci/manifests/{digest_segment}"


def oci_upload_chunk_key(session_id: str, sequence: int) -> str:
    """Key for one chunk of an in-progress upload.

    Chunks are stored individually and concatenated on completion because **no
    object store supports append**, and because the API is multi-replica: a
    chunked push can land on a different replica for each `PATCH`, so the
    partial cannot live on a pod-local disk. The ephemeral PVC — which the
    provider registry uses for streamed uploads — is unavailable here for
    exactly that reason.

    ``sequence`` is zero-padded so a lexicographic listing is also the
    concatenation order; every backend lists lexicographically, and relying on
    that avoids a second source of truth for ordering.
    """
    return f"oci/uploads/{session_id}/{sequence:08d}"


def oci_upload_prefix(session_id: str) -> str:
    """Prefix holding one upload session's chunks, for cleanup."""
    return f"oci/uploads/{session_id}/"
