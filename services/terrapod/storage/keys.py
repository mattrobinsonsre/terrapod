"""
Key path helpers for object storage.

Provides consistent key naming for all stored artifacts.
All keys are relative to the storage backend's configured prefix.
"""

from urllib.parse import quote


def state_index_key() -> str:
    """Key for the human-readable state index (break-glass DR recovery)."""
    return "state/index.yaml"


PULUMI_CHECKPOINT_PREFIX = "state/pulumi-checkpoints/"

#: Prefixes whose next path segment is a workspace id; derived-tested against the
#: workspace-scoped builders below.
WORKSPACE_KEY_PREFIXES: tuple[str, ...] = (
    "state/",
    "plans/",
    "logs/",
    "config/",
    "runs/",
    "inventory/",
    PULUMI_CHECKPOINT_PREFIX,
)


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


def workspace_id_of_key(key: str) -> str | None:
    """The workspace a key belongs to, or None for a shared key (incl. `state/index.yaml`
    and `state/deleted/` markers)."""
    if key.startswith(DELETED_MARKER_PREFIX):
        return None
    if key.startswith(PULUMI_CHECKPOINT_PREFIX):
        rest = key[len(PULUMI_CHECKPOINT_PREFIX) :]
    else:
        prefix = next((p for p in WORKSPACE_KEY_PREFIXES if key.startswith(p)), None)
        if prefix is None:
            return None
        rest = key[len(prefix) :]
    workspace_id, sep, _ = rest.partition("/")
    return workspace_id if sep and workspace_id else None


#: Prefixes Terrapod sweeps BY AGE with no key-shape filter, and the setting that
#: decides when. An object written here is deleted on that schedule whatever it is,
#: so a backup lasts until the sweep reaches it -- a different hazard from the
#: workspace prefixes above, which is why it earns its own refusal and its own
#: message rather than being folded into that list (#2621).
#:
#: Measured rather than assumed: `vcs_archives/` is the ONLY such sweep in the tree
#: (`artifact_retention_service`). `cache/`, `uploads/`, `registry/`, `oci/`,
#: `policies/` and `module_overrides/` hold Terrapod's own objects and are reasons
#: not to mix dumps in with them, but nothing sweeps them by age, and listing them
#: here would refuse a prefix on a hazard that does not exist.
AGE_SWEPT_PREFIXES: dict[str, str] = {
    "vcs_archives/": "vcs.archive_cache_retention_days",
}


def shared_prefix(setting: str, prefix: str) -> str:
    """`prefix` normalised to exactly one trailing slash and no leading `./` or `/`.

    Returns the NORMALISED form, and that is the point of it. It used to check the
    stripped spelling and return the one the operator wrote, so `./backups/` was
    accepted and handed back verbatim -- after which the behaviour DIVERGED BY
    BACKEND, which is the one thing a normaliser exists to erase (#2621). On the
    filesystem store `_full_path("./backups/x.dump")` writes to `root/backups/x.dump`
    (pathlib drops the `.`) while `list_prefix("./backups/")` filters on the literal
    and matches nothing, so retention never pruned, dumps accumulated for ever, and
    the DR drill reported "no backups found under './backups/'" on a store that
    demonstrably held them. S3, Azure and GCS keep the key verbatim and so stay
    self-consistent.

    ValueError when nothing is left once normalised, when it sits under a
    workspace-scoped prefix (its objects would be read as a workspace's), or when
    Terrapod sweeps it by age (its objects would be deleted on that schedule).
    """
    # The trailing slash goes on FIRST, then the leading junk comes off. The other
    # order leaves a bare "." as "./" -- it starts with neither "./" nor "/", so
    # nothing strips it, and the un-normalised spelling this exists to remove is
    # returned by the function that removes it.
    bare = prefix if prefix.endswith("/") else prefix + "/"
    while bare.startswith(("./", "/")):
        bare = bare[2:] if bare.startswith("./") else bare[1:]
    if not bare:
        raise ValueError(
            f"{setting}={prefix!r} names no prefix, so backups would be written to the "
            "store's root: the filesystem backend refuses an absolute key outright -- "
            "after a full pg_dump has run -- and the retention sweep would list every "
            "object in the store. Use a prefix of its own, such as 'backups/'"
        )
    if clash := next((p for p in WORKSPACE_KEY_PREFIXES if bare.startswith(p)), None):
        raise ValueError(
            f"{setting}={prefix!r} is under the workspace-scoped prefix {clash!r}, where "
            "its objects would be read as a workspace's. Use a prefix of its own, such "
            "as 'backups/' -- note that NOT being on this list is not the same as being "
            "safe; see the age-swept prefixes below"
        )
    if swept := next((p for p in AGE_SWEPT_PREFIXES if bare.startswith(p)), None):
        raise ValueError(
            f"{setting}={prefix!r} is under {swept!r}, which Terrapod sweeps by age on "
            f"{AGE_SWEPT_PREFIXES[swept]} with no filter on what the objects are -- so "
            "backups written there are deleted on that schedule. Use a prefix of its "
            "own, such as 'backups/'"
        )
    return bare


def prefix_may_hold_workspace_keys(prefix: str) -> bool:
    """Whether a listing of `prefix` can match a workspace-scoped key."""
    if prefix.startswith((DELETED_MARKER_PREFIX, state_index_key())):
        return False
    return any(prefix.startswith(p) or p.startswith(prefix) for p in WORKSPACE_KEY_PREFIXES)


def state_key(workspace_id: str, version_id: str) -> str:
    """Key for a workspace state version."""
    return f"state/{workspace_id}/{version_id}.tfstate"


def pulumi_checkpoint_key(workspace_id: str, update_id: str) -> str:
    """Key for a local Pulumi update's latest checkpoint, until it is promoted (#1564).

    Under `state/`, so it is state for replication and encryption. It is not under
    `state/{workspace_id}/`: restore reads every object there as a state version,
    and a checkpoint is not one until the update ends. The segment is not a uuid,
    so the orphaned-state reaper passes over it, and the `.json` suffix keeps it out
    of anything that looks for `.tfstate`.
    """
    return f"{PULUMI_CHECKPOINT_PREFIX}{workspace_id}/{update_id}.json"


def state_backup_key(workspace_id: str, version_id: str) -> str:
    """Key for a backup of a workspace state version."""
    return f"state/{workspace_id}/{version_id}.backup.tfstate"


def plan_log_key(workspace_id: str, run_id: str) -> str:
    """Key for a run's plan log output."""
    return f"logs/{workspace_id}/plans/{run_id}.log"


def apply_log_key(workspace_id: str, run_id: str) -> str:
    """Key for a run's apply log output."""
    return f"logs/{workspace_id}/applies/{run_id}.log"


def configure_log_key(workspace_id: str, run_id: str) -> str:
    """Key for a run's configure (Ansible) log output (#1988).

    A third key rather than a generic `{phase}` form, matching the two above.

    **EVERY configure writes here, composite or standalone.** This docstring
    used to say a composite's bytes are concatenated onto the APPLY log and that
    this key was for the standalone case; that was never what the runner did
    (corrected 2026-10-23, #2144). One Job runs exactly one phase --
    `job_entrypoint` dispatches on `cfg.phase` and `_run_configure_phase` has a
    single call site -- so a composite runs its own configure Job, whose
    `log_artifact_phase` is `configure` and which therefore uploads here.
    `run_reconciler` depends on that and says so: sending a configure's promoted
    log to the apply key "would have overwritten the apply's own log with it".

    The claim was wrong in the expensive direction. Reading it, a per-host log
    view had to be designed for an apply log that is part Terraform output and
    part tagged ansible output; measuring it, one read route serves every
    configure.

    Before it existed the live-stream endpoint silently relabelled an unknown
    phase as `plan` and wrote over the plan's own Redis key, so a configure's
    output appeared in the plan tab with nothing reporting an error. That
    coercion is now a refusal.
    """
    return f"logs/{workspace_id}/configures/{run_id}.log"


def plan_output_key(workspace_id: str, run_id: str) -> str:
    """Key for a run's binary plan output (tfplan file)."""
    return f"plans/{workspace_id}/{run_id}.tfplan"


def plan_json_output_key(workspace_id: str, run_id: str) -> str:
    """Key for a run's structured JSON plan output (`tofu show -json tfplan`)."""
    return f"plans/{workspace_id}/{run_id}.json-output"


def resolved_inventory_key(workspace_id: str, run_id: str) -> str:
    """Key for the inventory a configure run targets (#2021).

    Written by run creation, read by the configure phase with the run's own
    token. A run artifact rather than an inventory one, and the distinction is
    the whole point: the inventory itself is live -- resolution is a function of
    the declared rows and the run's configuration version, so a read of it
    moves -- while "what did THIS configure target" must not. Pinned here, it
    is also #1973's denominator for working out which hosts a partial configure
    left outstanding.

    Pinned to the COMMIT, not merely to the moment: the git half is extracted
    from the run's configuration version, so the inventory and the playbook come
    from the same commit even if the branch moves between launch and execution.
    """
    return f"runs/{workspace_id}/{run_id}.inventory.json"


def host_events_key(workspace_id: str, run_id: str) -> str:
    """Key for a configure's per-host, per-task event stream (#1928).

    Under `runs/` rather than `logs/`: this is a machine-read artifact of the run
    like the resolved inventory beside it, not the human log, and the prefix is
    already covered by the `run_vars` blob class so no new class is needed.

    The whole run's stream. `host_events_host_key` partitions the same events per
    host beside it, so a drill-down is one object fetch rather than a scan of
    this one.

    **An earlier revision of this docstring said the partition had been dropped
    because "the retention sweep deletes a FIXED key list per run and has no
    prefix enumeration, so N objects per run would leak for ever". That reason
    was wrong**, and it is worth recording why rather than quietly deleting it:
    the constraint is real and is what makes the oversized-payload sidecar one
    object per run, because a payload REFERENCE is an integer that exists only
    inside the stream, so nothing outside it can enumerate what was written. A
    HOST is not like that. `run_host_results` holds one row per host per run,
    nothing in the tree ever deletes those rows, and the sweep touches storage
    only -- so the host set is queryable at reap time and the keys derive from
    it. The two cases look alike and are not.
    """
    return f"runs/{workspace_id}/{run_id}.host-events.jsonl"


def host_events_host_key(workspace_id: str, run_id: str, host: str) -> str:
    """Key for ONE host's slice of the event stream (#1928).

    Written at completion alongside `host_events_key`, which is what makes a
    per-host drill-down a single object fetch instead of fetching the whole
    run's stream and filtering it. At fleet scale that is the difference between
    one host's ~100 KB and a stream of tens of MB.

    The host is percent-encoded with nothing safe but the unreserved set, so a
    name carrying a `/` cannot escape the run's prefix and invent a key under
    another run. `.` stays literal, so an ordinary FQDN remains readable in a
    bucket listing.

    Deletable despite not being in `RUN_ARTIFACT_KEYS`, whose entries take only
    `(workspace_id, run_id)`: the retention sweep derives the host set from this
    run's `run_host_results` rows, which outlive every artifact. See
    `host_events_key` for why that works here and not for the payload sidecar.
    """
    return f"runs/{workspace_id}/{run_id}.host-events.{quote(host, safe='')}.jsonl"


def host_events_large_key(workspace_id: str, run_id: str) -> str:
    """Key for the oversized payloads `host_events_key` references by id.

    A single `gather_facts` result is 16.5 KB per host (measured on ansible-core
    2.18.3; a Linux host with more mounts and NICs is larger), so the spill
    threshold sits above it deliberately -- facts are the normal case. What lands
    here is the exception: on one probe, three 140 KB command results moved
    422 KB out of a stream that a host filter would otherwise have to scan,
    leaving the main object at 62 KB.

    ONE sidecar per run, not one per payload, for the retention reason in
    `host_events_key`.
    """
    return f"runs/{workspace_id}/{run_id}.host-events-large.jsonl"


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


# --- Operator-uploaded artifacts (#2599) ---
#
# Under their OWN prefix rather than beside the pull-through content they stand
# in for, and the reason is replication rather than tidiness. `blob_classes`
# resolves a key to exactly one class by longest-prefix match, deliberately --
# `owning_class`'s docstring says overlapping classes would be "counted twice in
# a readiness report, copied twice by the copier", and it is the gate that stops
# a crafted key escaping a prefix. So two classes cannot share `cache/packages/`,
# and the distinction has to live in the key.
#
# It has to live SOMEWHERE because the two differ in the one way replication
# cares about: pull-through content is re-warmable and an uploaded artifact is
# not. There is no upstream to re-fetch it from, sealed or otherwise, so a
# promoted follower that treated it as re-derivable would hold a complete ledger
# of what should be there and be unable to serve any of it -- recoverable only by
# the operator re-uploading their whole curated set, discovered during an
# incident.
#
# Safe to introduce because every serve path reads `record.storage_key` rather
# than recomputing a key, so an object's location is whatever its row says: new
# uploads land here, rows written before this keep serving from where they are,
# and a backfill relocates them.

UPLOADS_PREFIX = "uploads/"


# An operator upload goes under its own top-level prefix rather than beside the
# pull-through content it stands in for, and the reason is `blob_classes`:
# `owning_class` resolves exactly ONE class per key by longest-prefix match --
# deliberately, because overlapping classes would be counted twice in a
# readiness report and copied twice by the copier, and it is also the gate that
# stops a crafted key escaping a prefix. So two classes cannot share
# `cache/binaries/`, and the distinction between "re-fetchable from upstream"
# and "irreplaceable" has to live in the key itself (#2599).
#
# Safe because every serve path reads the row's recorded `storage_key` rather
# than deriving one -- see `services.cache_object_keys`.


def uploaded_package_key(ecosystem: str, name: str, filename: str) -> str:
    """Where an operator-uploaded language package lives."""
    return f"{UPLOADS_PREFIX}packages/{ecosystem}/{name}/{filename}"


def uploaded_provider_key(
    hostname: str, namespace: str, type_: str, version: str, filename: str
) -> str:
    """Where an operator-uploaded provider archive lives."""
    return f"{UPLOADS_PREFIX}providers/{hostname}/{namespace}/{type_}/{version}/{filename}"


def uploaded_binary_key(tool: str, version: str, os_: str, arch: str) -> str:
    """Where an operator-uploaded engine binary lives."""
    return f"{UPLOADS_PREFIX}binaries/{tool}/{version}/{os_}_{arch}"


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


def inventory_baseline_key(workspace_id: str) -> str:
    """The resolved inventory the workspace's last SUCCESSFUL configure targeted.

    The baseline a derived configure narrows against (#2229): diffing the current
    resolution against this gives the hosts whose effective configuration has
    moved since anything was actually applied to them.

    **One object per workspace, overwritten** -- not a version history. The
    inventory re-cut removed `inventory_versions` deliberately, because every
    source is static so a read resolves live and there is nothing to keep. What
    narrowing needs is far smaller: one retained dump. The user's framing: *if we
    have a repeatable inventory dump process, which we do, we don't have to keep
    the old database records, just at least one old dump.*

    **Keyed on the workspace, NOT on a run, and that is load-bearing.** The run's
    own `resolved_inventory_key` is in `RUN_ARTIFACT_KEYS`, and a successful
    configure is `applied`, which `REAPABLE_RUN` admits -- so that artifact is
    deleted after `run_artifacts_retention_days` (90 by default) and a workspace
    configuring less often than that would lose its baseline between runs. The
    failure would be quiet rather than wrong (no baseline means "configure
    everything"), which is exactly why it would go unnoticed. One object per
    workspace is bounded by construction and never reaped.

    Written on SUCCESS, so "the last successful configure" is literally true: a
    failed configure leaves the previous baseline standing, which is correct
    because its hosts may be unconfigured and the next run should go wide anyway.

    NOT REPLICATED. `inventory/` is a workspace key prefix with no blob class --
    `blob_classes.CLASSES` covers seventeen prefixes and this is not one of them
    -- so a promoted follower has no baseline and its first configure of each
    workspace goes wide. That is the safe direction (no baseline means
    "configure everything") and it is why the gap is quiet, which is the reason
    to say so here rather than leave it to be re-derived. Pre-existing from
    #2229; recorded while auditing #2583's prefix table.
    """
    return f"inventory/{workspace_id}/baseline.json"
