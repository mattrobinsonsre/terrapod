# Artifact Retention & Cleanup

Terrapod stores versioned state files, run logs, plan outputs, configuration tarballs, cached binaries, and cached providers in object storage. Without cleanup, storage grows unboundedly. The artifact retention system automatically purges old artifacts while preserving safety invariants.

---

## Overview

When enabled, a daily background task (via the distributed scheduler) iterates six artifact categories and deletes entries that exceed their configured retention threshold. Each category runs independently -- a failure in one does not block the others.

| Category | Retention Key | Default | What Gets Deleted |
|---|---|---|---|
| **State versions** | `state_versions_keep` | 20 per workspace | Excess state versions beyond the keep count (oldest first) |
| **Run artifacts** | `run_artifacts_retention_days` | 90 days | Plan output, plan logs, and apply logs for terminal runs |
| **Config versions** | `config_versions_retention_days` | 90 days | Uploaded configuration tarballs no longer referenced by active runs |
| **Provider cache** | `provider_cache_retention_days` | 30 days | Cached upstream provider binaries not accessed within the retention window |
| **Binary cache** | `binary_cache_retention_days` | 30 days | Cached terraform/tofu CLI binaries not accessed within the retention window |
| **Module overrides** | `module_overrides_retention_days` | 14 days | Module impact analysis override tarballs from completed speculative runs |

Set any per-category value to `0` to disable cleanup for that category.

---

## Configuration

### Helm Values

```yaml
api:
  config:
    artifact_retention:
      enabled: true                         # enabled by default
      poll_interval_seconds: 86400          # how often the cleanup runs (default: daily)
      batch_size: 100                       # max items processed per category per cycle
      state_versions_keep: 20              # state versions to keep per workspace
      run_artifacts_retention_days: 90     # days before run logs/plans are deleted
      config_versions_retention_days: 90   # days before config tarballs are deleted
      provider_cache_retention_days: 30    # days since last access before provider cache entry is deleted
      binary_cache_retention_days: 30      # days since last access before binary cache entry is deleted
      module_overrides_retention_days: 14  # days before module override tarballs are deleted
```

### Enabled by Default

Artifact retention is enabled by default. The cleanup task registers with the distributed scheduler and runs at the configured interval. It is multi-replica safe -- exactly one replica executes each cycle regardless of how many API replicas are running. To disable, set `artifact_retention.enabled: false`.

### Tuning

- **`batch_size`** controls the maximum number of items processed per category per cycle. Lower values reduce database load but may take multiple cycles to clean up a backlog. Higher values clean up faster but hold database connections longer.
- **`poll_interval_seconds`** defaults to 86400 (daily). For large deployments with aggressive retention, consider running more frequently (e.g. 43200 for twice daily).
- **Per-category days** can be tuned independently. Set any to `0` to disable that category entirely.

---

## Safety Invariants

The retention system enforces several invariants to prevent data loss:

### State Versions

- **The latest state version per workspace is never deleted**, even if `state_versions_keep` is set to 0. The highest-serial state version is always preserved.
- **Workspaces with `state_diverged=True` are skipped entirely.** When a runner Job fails to upload state after a successful apply, the workspace is flagged as state-diverged. The operator may need all historical state versions for recovery, so retention skips these workspaces until the divergence is resolved.
- Versions are ordered by serial (descending) and the excess beyond `state_versions_keep` is deleted.

### Run Artifacts

- **Only terminal runs are eligible.** Runs in `applied`, `errored`, `discarded`, or `canceled` state have their artifacts cleaned up. Runs still in progress (`pending`, `queued`, `planning`, `planned`, `confirmed`, `applying`) are never touched.
- Artifacts deleted: plan output file, plan-phase log, apply-phase log.

### Config Versions

- **Config versions referenced by non-terminal runs are preserved.** A configuration tarball is only eligible for deletion if no active run references it. This prevents removing config that a running plan/apply needs.

### Cache Entries (Provider & Binary)

- **Retention is based on last access time, not creation time.** A cached binary or provider that is actively used by runners will have its `last_accessed_at` timestamp refreshed on every cache hit. Only entries that haven't been accessed within the retention window are deleted.
- This ensures frequently-used cache entries are preserved regardless of age, while entries that were cached once and never used again are cleaned up.

### Module Overrides

- **Only overrides from terminal runs are deleted.** Active speculative runs retain their override tarballs.
- The `module_overrides` JSONB field on the run is set to `NULL` after storage objects are deleted.

### Best-Effort Deletes

All storage deletions are wrapped in try/except blocks. If an individual object deletion fails (e.g. transient storage error), the error is logged and the cycle continues. The `terrapod_retention_errors_total` counter is incremented for observability.

---

## Access-Based Cache Retention

Both the provider cache and binary cache track when each entry was last accessed via a `last_accessed_at` column. This timestamp is updated every time a cache hit occurs:

- **Binary cache**: When a runner downloads a cached terraform/tofu binary, the `last_accessed_at` on the `CachedBinary` record is touched.
- **Provider cache**: When the `{version}.json` endpoint serves cached platform info (tier 1 lookup), `last_accessed_at` is touched on each `CachedProviderPackage` record. Similarly, when a single platform is served via the download proxy, the timestamp is updated.

The retention cleanup compares `last_accessed_at` against the configured retention days -- not `cached_at`. This means:

- A provider cached 6 months ago but accessed yesterday is **kept**.
- A provider cached 2 days ago but never accessed again is **deleted** after 30 days of inactivity.

This design avoids purging high-traffic cache entries that would immediately be re-fetched from upstream.

---

## Monitoring

Three Prometheus metrics track retention activity:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `terrapod_retention_deleted_total` | Counter | category | Artifacts successfully deleted |
| `terrapod_retention_errors_total` | Counter | category | Per-item deletion errors |
| `terrapod_retention_duration_seconds` | Histogram | -- | Wall-clock duration of the full cleanup cycle |

Categories: `state_versions`, `run_artifacts`, `config_versions`, `provider_cache`, `binary_cache`, `module_overrides`.

### Recommended Alert

```yaml
- alert: TerrapodRetentionErrors
  expr: increase(terrapod_retention_errors_total[1d]) > 10
  for: 1h
  annotations:
    summary: "Artifact retention encountering persistent errors"
    description: "More than 10 retention deletion errors in the last day. Check storage backend health."
```

### Scheduler Visibility

The retention task appears in the scheduler metrics as `artifact_retention`:

```promql
# Last execution status
terrapod_scheduler_task_executions_total{task="artifact_retention"}

# Execution duration
terrapod_scheduler_task_duration_seconds{task="artifact_retention"}
```

---

## Interaction with Disaster Recovery

The artifact retention system is designed to coexist safely with the [break-glass state recovery](disaster-recovery.md) workflow:

- **State versions are not aggressively purged.** The default `state_versions_keep: 20` preserves the 20 most recent versions per workspace. The latest version (used by the state index for recovery) is always preserved.
- **State-diverged workspaces are fully exempt.** If a workspace enters state-diverged status (failed state upload after apply), all its state versions are preserved until the operator resolves the issue.
- **The state index (`state/index.yaml`) is never modified by retention.** It always points to the latest state version, which is never deleted.

If you need to preserve more state history for compliance or audit, increase `state_versions_keep` or set it to `0` to disable state version cleanup entirely.

---

## Storage Impact

To estimate storage savings, consider:

| Artifact Type | Typical Size | Growth Rate |
|---|---|---|
| State version | 10 KB -- 10 MB | Per apply (varies by workspace) |
| Run logs (plan + apply) | 50 KB -- 5 MB | Per run |
| Config tarball | 1 KB -- 50 MB | Per run (VCS or CLI upload) |
| Provider binary | 20 -- 200 MB | Per unique provider/version/platform |
| CLI binary | 50 -- 100 MB | Per unique tool/version/platform |
| Module override tarball | 1 KB -- 50 MB | Per module-test speculative run |

For a deployment running 100 applies/day with 50 workspaces, default retention settings (90-day run artifacts, 20 state versions) would retain approximately:

- ~9,000 run artifact sets (plan + logs)
- ~1,000 state versions (20 x 50 workspaces)
- Cache entries based on access patterns (only unused entries purged)

---

## Example: Production Configuration

```yaml
api:
  config:
    artifact_retention:
      enabled: true
      poll_interval_seconds: 86400          # daily
      batch_size: 200                       # larger batch for faster cleanup
      state_versions_keep: 50              # keep more history for compliance
      run_artifacts_retention_days: 180    # 6 months of run logs
      config_versions_retention_days: 90   # 3 months of config tarballs
      provider_cache_retention_days: 60    # keep popular providers longer
      binary_cache_retention_days: 60      # keep popular binaries longer
      module_overrides_retention_days: 7   # clean up speculative overrides quickly
```

---

## Example: Minimal Retention (Cost-Sensitive)

```yaml
api:
  config:
    artifact_retention:
      enabled: true
      poll_interval_seconds: 43200          # twice daily
      batch_size: 500                       # aggressive cleanup
      state_versions_keep: 5               # minimum useful history
      run_artifacts_retention_days: 30     # 1 month of run logs
      config_versions_retention_days: 30   # 1 month of config tarballs
      provider_cache_retention_days: 14    # 2 weeks of cache inactivity
      binary_cache_retention_days: 14      # 2 weeks of cache inactivity
      module_overrides_retention_days: 3   # clean up quickly
```
