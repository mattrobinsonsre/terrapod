# Drift Detection

Drift detection automatically discovers out-of-band infrastructure changes by running scheduled plan-only runs across workspaces. When terraform reports that the real-world state differs from the stored state, Terrapod marks the workspace as "drifted" and optionally fires notifications.

![Workspace Overview](images/workspace-overview.png)

---

## How It Works

1. A background scheduler task (`drift_check`) runs every `poll_interval_seconds` (default 300s)
2. For each workspace with drift detection enabled, it checks whether the configured interval has elapsed
3. If due, it creates a **plan-only run** with `is_drift_detection=true`
4. The runner executes `terraform plan` and reports whether changes were detected
5. A triggered task (`drift_run_completed`) updates the workspace's `drift_status` based on the result
6. If drift is detected, a `run:drift_detected` notification is enqueued

Drift runs are **plan-only** — they never apply changes. An admin must review the plan output and decide whether to apply.

---

## Configuration

### Platform-Level

Drift detection is enabled by default at the platform level. Per-workspace settings control which workspaces are checked.

| Setting | Default | Description |
|---|---|---|
| `drift_detection.enabled` | `true` | Enable the drift detection scheduler task |
| `drift_detection.poll_interval_seconds` | `300` | How often the scheduler scans for workspaces due for a check |
| `drift_detection.min_workspace_interval_seconds` | `3600` | Floor for per-workspace check intervals (prevents excessive checking) |

Helm values:

```yaml
api:
  config:
    drift_detection:
      enabled: true
      poll_interval_seconds: 300
      min_workspace_interval_seconds: 3600
```

Environment variables:

```bash
TERRAPOD_DRIFT_DETECTION__ENABLED=true
TERRAPOD_DRIFT_DETECTION__POLL_INTERVAL_SECONDS=300
TERRAPOD_DRIFT_DETECTION__MIN_WORKSPACE_INTERVAL_SECONDS=3600
```

### Per-Workspace

Each workspace has independent drift detection settings:

| Field | Default | Description |
|---|---|---|
| `drift_detection_enabled` | `true` (VCS-connected) / `false` (non-VCS) | Enable drift detection for this workspace |
| `drift_detection_interval_seconds` | `86400` (24h) | How frequently to check (clamped to platform minimum) |

Drift detection is **automatically enabled** when a workspace is created with a VCS connection, or when a VCS connection is added to an existing workspace. It can be explicitly overridden in either case.

Enable via the API:

```bash
curl -X PATCH https://terrapod.local/api/v2/organizations/default/workspaces/ws-abc123 \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "workspaces",
      "attributes": {
        "drift-detection-enabled": true,
        "drift-detection-interval-seconds": 3600
      }
    }
  }'
```

Or toggle it in the workspace overview UI — the Drift Detection card has an enable/disable toggle and an interval selector.

---

## Drift Status

Each workspace tracks its current drift status:

| Status | Meaning |
|---|---|
| _(empty)_ | Unchecked — drift detection enabled but no check has run yet |
| `no_drift` | Latest plan found no changes |
| `drifted` | Latest plan detected infrastructure changes |
| `errored` | Latest plan failed (runner error, config error, etc.) |

The status is updated after each drift run completes. The mapping from run outcome to drift status:

| Run Status | `has_changes` | Resulting `drift_status` |
|---|---|---|
| `planned` | `true` | `drifted` |
| `planned` | `false` | `no_drift` |
| `planned` | `null` | `drifted` (conservative) |
| `errored` | — | `errored` |
| `canceled` / `discarded` | — | No update |

### Dismissing drift

When a workspace shows `drifted` or `errored` status — for example after you've acknowledged the drift and plan to reconcile it through a real apply — you can clear the reported state without disabling scheduled drift detection.

**UI**: click the "Dismiss" link next to the drift-status badge on the workspace overview.

**API**:

```http
POST /api/v2/workspaces/{workspace_id}/actions/dismiss-drift
```

Effect:

- `drift_status` → `""` (unchecked)
- `drift_last_checked_at` → `null`
- `drift_detection_enabled` — **unchanged** (scheduled checks continue)

The next scheduled check repopulates the status from current infrastructure reality. If drift is still present, the status flips back to `drifted`.

Idempotent: dismissing a workspace that isn't currently reporting drift is a no-op.

Requires `plan` permission on the workspace.

---

## Skipping Logic

The drift checker skips a workspace if any of the following are true:

- **Not yet due** — elapsed time since last check is less than the configured interval
- **Workspace locked** — a user has an active lock
- **Active runs** — the workspace has runs in non-terminal states (pending, queued, planning, planned, confirmed, applying)
- **No state** — the workspace has zero state versions (nothing to drift against)
- **VCS error** — (VCS-connected workspaces) the connection is inactive, repo URL is unparseable, or archive download fails

---

## VCS-Connected Workspaces

For workspaces with a VCS connection, drift runs download the latest configuration from the tracked branch before planning. This ensures the plan compares against the current codebase, not stale configuration.

For non-VCS workspaces, drift runs use the latest uploaded `ConfigurationVersion`.

---

## Notification Integration

When `drift_status` changes to `drifted`, a `run:drift_detected` notification trigger fires. Configure a [notification](notifications.md) on the workspace with this trigger to receive alerts via webhook, Slack, or email.

---

## Workspace Health Conditions

Drift status is visible on the workspace overview tab as a health condition banner. The workspace list page aggregates health issues (including drift) into summary cards, showing at a glance how many workspaces have detected drift or other health concerns.

---

## Multi-Replica Safety

Drift detection uses the [distributed scheduler](architecture.md) — only one API replica runs the check cycle per interval. No leader election is needed; Redis provides mutual exclusion.

---

## See Also

- [Notifications](notifications.md) — configure alerts for drift detection events
- [Architecture](architecture.md) — scheduler and background task system
- [API Reference](api-reference.md) — workspace API with drift detection fields
