# API Reference

Terrapod implements the TFE V2 API for compatibility with the `terraform` CLI, `go-tfe` client, and existing CI/CD integrations. All endpoints use JSON:API format.

The interactive API documentation is also available in the web UI under **API** in the navigation bar, offering both ReDoc and Swagger UI views.

![API Documentation (ReDoc)](images/api-docs-redoc.png)

---

## Consumers

The Terrapod API has four distinct consumer classes. Every API change must update every affected consumer — this is a hard contract:

1. **Web UI** (`web/`) — Next.js BFF + React frontend. Server-side SSR + client-side `fetch()`. Frontend changes when JSON:API attribute names, endpoint paths, or response shapes change.
2. **go-terrapod** (`go-terrapod/`) — the **canonical Go SDK**. Strongly-typed methods over the Terrapod JSON:API surface. Single source of truth for the Go view of the API: the provider and migration tool both import it; third-party Go automation can import it directly (`github.com/mattrobinsonsre/terrapod/go-terrapod`). Same shape and stability story as `hashicorp/go-tfe`.
3. **terraform-provider-terrapod** (`provider/`) — a **thin wrapper around go-terrapod**. The provider holds only Terraform-plugin-framework code (schema, state translation, lifecycle hooks); every API call goes through go-terrapod. No JSON:API marshalling code lives inside `provider/internal/`.
4. **terrapod-migrate** (`migrate/`) — the **migration tool** that moves TFE/HCP + Atlantis platforms onto Terrapod. Reads from the source via `go-tfe` (TFE migrations) or local-clone HCL parsing (Atlantis migrations). Writes to Terrapod via go-terrapod. Distributed as a universal-macOS + linux/windows amd64+arm64 GitHub Release artifact.

### Workflow for extending the API

1. Add the endpoint to the appropriate Python router (`services/terrapod/api/routers/*.py`).
2. Add a typed method on go-terrapod (`go-terrapod/<resource>.go`) + tests.
3. Update each consumer that needs it: the provider's resource file, the frontend page, or the migration tool's writer.

### Endpoint coverage

go-terrapod targets the full Terrapod API surface — both the TFE-V2-compatible (`/api/v2/`) and Terrapod-native (`/api/terrapod/v1/`) prefixes. The migration tool is a heavy consumer of the API-only routers (config-versions, state-management, registry endpoints) that the UI doesn't surface; the provider mostly consumes the frontend-also routers. Both can rely on the same typed surface in go-terrapod.

### Version contract

go-terrapod pins to a specific Terrapod API version at build time via the `SDKVersion` constant. Consuming tools call `Client.VersionCheck` at startup to fail-fast on a mismatch. The migration tool requires this match by default and exposes `--allow-api-version-mismatch` for advanced operators. Releases of Terrapod, go-terrapod, the provider, and the migration tool all happen at the same tag — they ship together.

---

## Base URL and Authentication

### Base URL

Terrapod exposes two API surfaces:

| Prefix | Contract | Audience |
|---|---|---|
| `/api/v2/` | **Stable** TFE V2 subset consumed by `terraform`, `tofu`, and `tfci`. Documented in [`docs/tfe-cli-surface.md`](tfe-cli-surface.md). |  Terraform/OpenTofu CLI, `tfci`, `go-tfe`-based clients |
| `/api/terrapod/v1/` | Terrapod-native management API (workspaces management, runs, registry CRUD, agent pools, audit, etc.) | Web UI, the Terraform provider for Terrapod, automation |

Example:

```
# CLI-contract (cloud-block, state, etc.)
https://terrapod.example.com/api/v2/organizations/default/workspaces

# Terrapod-native management
https://terrapod.example.com/api/terrapod/v1/workspaces
```

A handful of endpoints (e.g. `/health`, `/ready`, `/.well-known/terraform.json`, OAuth flows, `/v1/...` registry CLI protocol) live at the root for protocol-compatibility reasons.

### Authentication

Include a Bearer token in the `Authorization` header:

```
Authorization: Bearer <api-token-or-session-token>
```

API tokens are obtained via `terraform login`, the web UI, or the token creation endpoint. Session tokens are obtained via the login flow.

### Content Type

Requests with a body should use:

```
Content-Type: application/vnd.api+json
```

Responses use `application/json` (accepted by `go-tfe`).

### Organization

Terrapod is single-organization. The literal organization name `default` is the only valid value; every API path that contains an organization segment uses `organizations/default/` verbatim. Requests to any other organization name return 404.

---

## Health Check Endpoints

### Liveness Probe

```
GET /health
```

Returns 200 if the API process is running.

**Response:**
```json
{"status": "healthy"}
```

### Readiness Probe

```
GET /ready
```

Checks database, Redis, and storage subsystems. Returns 200 if all healthy, 503 otherwise.

**Response (healthy):**
```json
{
  "status": "ready",
  "checks": {
    "database": "healthy",
    "redis": "healthy",
    "storage": "healthy"
  }
}
```

---

## Service Discovery

### Terraform Service Discovery

```
GET /.well-known/terraform.json
```

Returns service discovery document for `terraform login` and registry protocol.

**Response:**
```json
{
  "login.v1": {
    "client": "terraform-cli",
    "grant_types": ["authz_code"],
    "authz": "/oauth/authorize",
    "token": "/oauth/token",
    "ports": [10000, 10010]
  },
  "modules.v1": "/api/terrapod/v1/registry/modules/",
  "providers.v1": "/api/terrapod/v1/registry/providers/"
}
```

---

## Ping

```
GET /api/v2/ping
```

API version handshake. Returns TFE-compatible version headers.

**Response headers:**
```
TFP-API-Version: 2.6
TFP-AppName: Terrapod
X-TFE-Version: v0.1.0
```

---

## Account

### Current User Details

```
GET /api/v2/account/details
```

Returns the authenticated user's information.

**Response:**
```json
{
  "data": {
    "id": "user-abc123",
    "type": "users",
    "attributes": {
      "username": "alice@example.com",
      "email": "alice@example.com",
      "is-service-account": false
    }
  }
}
```

---

## Organizations

### Show Organization

```
GET /api/terrapod/v1/organizations/default
```

Returns organization details. Only `default` is valid.

### Entitlement Set

```
GET /api/v2/organizations/default/entitlement-set
```

Returns feature flags (all enabled for Terrapod).

---

## Workspaces

### List Workspaces

```
GET /api/v2/organizations/default/workspaces
```

### Get Workspace by Name

```
GET /api/v2/organizations/default/workspaces/{name}
```

### Get Workspace by ID

```
GET /api/v2/workspaces/{id}
```

### Create Workspace

```
POST /api/v2/organizations/default/workspaces
```

**Request body:**
```json
{
  "data": {
    "type": "workspaces",
    "attributes": {
      "name": "my-workspace",
      "auto-apply": false,
      "execution-mode": "agent",
      "terraform-version": "1.9.8",
      "resource-cpu": "1",
      "resource-memory": "2Gi",
      "labels": {
        "env": "dev",
        "team": "platform"
      },
      "vcs-repo-url": "https://github.com/org/repo",
      "vcs-branch": "main",
      "working-directory": "terraform/",
      "drift-detection-enabled": false,
      "drift-detection-interval-seconds": 86400
    },
    "relationships": {
      "vcs-connection": {
        "data": {
          "id": "vcs-abc123",
          "type": "vcs-connections"
        }
      }
    }
  }
}
```

**Required permission:** Any authenticated user can create workspaces (creator becomes owner).

### Update Workspace

```
PATCH /api/v2/workspaces/{id}
```

Same body format as create. Only include attributes to change.

**Required permission:** `admin` on the workspace.

**Self-lockout protection:** If the request changes `labels` and the new labels would reduce the caller's own access level, the API returns **409 Conflict** with a descriptive error. Re-submit with `"force": true` in the attributes to confirm the change. Platform admins and workspace owners are immune (their access doesn't depend on labels).

### Workspace Permissions Block

All workspace responses (show and list) include a `permissions` object reflecting the authenticated user's effective permissions:

```json
{
  "permissions": {
    "can-update": true,
    "can-destroy": true,
    "can-queue-run": true,
    "can-read-state-versions": true,
    "can-create-state-versions": true,
    "can-read-variable": true,
    "can-update-variable": true,
    "can-lock": true,
    "can-unlock": true,
    "can-force-unlock": true,
    "can-read-settings": true
  }
}

### Delete Workspace

```
DELETE /api/terrapod/v1/workspaces/{id}
```

**Required permission:** `admin` on the workspace.

### Lock Workspace

```
POST /api/v2/workspaces/{id}/actions/lock
```

**Required permission:** `plan` on the workspace.

### Unlock Workspace

```
POST /api/v2/workspaces/{id}/actions/unlock
```

**Required permission:** `plan` on the workspace (own locks only).

### Drift Detection Attributes

Workspaces support the following drift detection attributes (settable on create and update):

| Attribute | Type | Default | Description |
|---|---|---|---|
| `drift-detection-enabled` | boolean | `true` (VCS) / `false` (non-VCS) | Enable or disable automatic drift detection. Auto-enabled when a VCS connection is set |
| `drift-detection-interval-seconds` | integer | `86400` | How often to run drift detection checks (minimum: 3600 seconds / 1 hour) |
| `drift-ignore-rules` | list[string] | `[]` | Glob-aware patterns silenced by the drift-result classifier (#482). Each rule is a Terraform address optionally suffixed with a dotted attribute path; `*` matches zero or more non-`.` chars (spans `[N]` indices), `[*]` matches any bracketed index. A bare address with no attribute suffix silences any change to that resource — including destroys — so use carefully. Max 50 entries, ≤ 500 chars each. Examples: `aws_iam_role.foo.tags.Environment`, `aws_autoscaling_group.workers[*].desired_capacity`, `module.eks*.argocd_cluster.*.config.tls_client_config.ca_data`. Affects drift-detection runs only — regular plan/apply is untouched. See [drift-ignore-rules.md](drift-ignore-rules.md) for the full grammar and recipes |

### AI Plan Summary Attributes

Workspaces carry two attributes that govern the optional AI plan-summary feature (settable on create and update). When the feature is globally disabled at the deployment level (`api.config.ai_summary.enabled: false`), these fields are stored but inert — no calls are made. See [docs/ai-plan-summary.md](ai-plan-summary.md) for the full operator guide.

| Attribute | Type | Default | Description |
|---|---|---|---|
| `ai-summary-mode` | string | `"default"` | Per-workspace override. One of `"default"` (follow the global toggle), `"enabled"` (always summarise this workspace's plans), or `"disabled"` (never summarise this workspace — overrides global). |
| `ai-summary-context` | string | `""` | Free text up to 4000 characters appended to the model's prompt as workspace-specific facts (e.g. "Fronts the vault for service X — destroying the KMS key causes a global outage."). Additive to the deployment-wide `fleet_context`. |

422 errors:
- `ai-summary-mode` outside the enum
- `ai-summary-context` longer than 4000 characters
- `ai-summary-context` not a string

The following read-only attributes are included in workspace responses when drift detection is enabled:

| Attribute | Type | Description |
|---|---|---|
| `drift-last-checked-at` | string (RFC3339) or null | Timestamp of the last completed drift detection check |
| `drift-status` | string | Current drift status: `""` (never checked), `"no_drift"`, `"drifted"`, or `"errored"` |
| `drift-latest-run-id` | string (run-…) or null | ID of the drift run that produced the current `drift-status`. Lets the workspace-list UI link the badge straight to the run that explains the status. Cleared on a successful (non-drift) apply because the previous drift run is no longer the canonical link. Null when drift detection has never run or when the column predates v0.35.3 |

### Lifecycle Attributes (Autodiscovery)

Workspaces created by autodiscovery carry read-only lifecycle attributes that track rename/delete/orphan reconciliation. They are included in workspace responses and surfaced by the UI as a banner on the workspace detail page and a badge on the workspace list.

| Attribute | Type | Description |
|---|---|---|
| `lifecycle-state` | string | `"active"` (normal), `"pending_deletion"` (the source directory was removed or the workspace was orphaned and the rule did not opt in to destroy — needs an explicit operator action), or `"archived"` (terminal: never-applied orphan auto-archived, or destroyed via an opt-in `destroy` rule, or a superseded speculative rename duplicate) |
| `lifecycle-reason` | string | Human-readable explanation of the current state (e.g. `directory 'accounts/x' removed on 'main'`, `origin PR #14 closed unmerged; never applied — auto-archived`). Empty for `active` workspaces |
| `autodiscovery-pr-number` | integer or null | The PR that created the workspace, while it is still speculative. Cleared (graduated) once that PR merges; `null` for non-autodiscovered workspaces |

The owning autodiscovery rule's `on-directory-delete` policy (`flag` default, or opt-in `destroy`) governs what happens when a tracked directory is removed — see [autodiscovery.md](autodiscovery.md). A rename of a tracked directory moves the existing workspace in place (state and history preserved); it never destroys, even on a `destroy` rule.

### State Divergence Flag

Workspaces also expose a read-only `state-diverged` boolean. It is set to `true` when the runner reports it could not upload state after a successful apply (the apply ran against the real provider, but the corresponding state version did not land in Terrapod), so Terrapod's view of the workspace state and the real-world infrastructure may have drifted. The UI surfaces this as a banner on the workspace detail page. Clear it by running a fresh apply or by manually uploading a state version that matches reality.

```json
"state-diverged": false
```

### List VCS Refs (Terrapod Extension)

```
GET /api/terrapod/v1/workspaces/{id}/vcs-refs
```

Returns branches, tags, and the default branch for a VCS-connected workspace. Used by the UI to populate the VCS ref picker when queueing runs.

**Required permission:** `read` on the workspace.

**Response:**
```json
{
  "branches": [
    {"name": "main", "sha": "abc123..."},
    {"name": "feature-x", "sha": "def456..."}
  ],
  "tags": [
    {"name": "v1.0.0", "sha": "789abc..."}
  ],
  "default-branch": "main"
}
```

Returns 422 if the workspace is not VCS-connected or the VCS connection is inactive.

---

## State Versions

### List State Versions

```
GET /api/v2/workspaces/{id}/state-versions
```

**Required permission:** `read` on the workspace.

### Current State Version

```
GET /api/v2/workspaces/{id}/current-state-version
```

**Required permission:** `read` on the workspace.

For **agent-mode runs** reading another workspace's state via `data "terraform_remote_state"` (runner-token principals), authorization is by the producer-controlled consumer allowlist instead of per-user RBAC — see [Cross-Workspace Remote-State Consumers](#cross-workspace-remote-state-consumers) and [the composition guide](remote-state.md).

### Create State Version

```
POST /api/v2/workspaces/{id}/state-versions
```

**Request body:**
```json
{
  "data": {
    "type": "state-versions",
    "attributes": {
      "serial": 1,
      "md5": "d41d8cd98f00b204e9800998ecf8427e",
      "lineage": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    }
  }
}
```

**Required permission:** `write` on the workspace.

### Show State Version

```
GET /api/v2/state-versions/{id}
```

### Download State

```
GET /api/v2/state-versions/{id}/download
```

Returns a redirect to a presigned URL for the raw state file.

**Required permission:** `plan` on the workspace.

For **agent-mode runs** reading another workspace's state via `data "terraform_remote_state"` (runner-token principals), authorization is by the producer-controlled consumer allowlist instead of per-user RBAC — see [Cross-Workspace Remote-State Consumers](#cross-workspace-remote-state-consumers).

### Upload State Content

```
PUT /api/v2/state-versions/{id}/content
```

Binary upload of raw state bytes. No auth required (presigned-style -- the state version UUID acts as a capability token). This matches `go-tfe` behavior.

### Upload JSON State Content

```
PUT /api/v2/state-versions/{id}/json-content
```

Accepted and discarded (placeholder for future use).

### State Version Response Format

State version responses include a `created-by` attribute (email of the user who created it, or null for runner-created states) and a `run` relationship linking to the run that produced the state:

```json
{
  "data": {
    "id": "sv-...",
    "type": "state-versions",
    "attributes": {
      "serial": 1,
      "lineage": "...",
      "md5": "...",
      "size": 1234,
      "created-at": "2026-01-01T00:00:00Z",
      "created-by": "user@example.com"
    },
    "relationships": {
      "run": {
        "data": { "id": "run-...", "type": "runs" }
      }
    }
  }
}
```

### Delete State Version (Terrapod Extension)

```
DELETE /api/terrapod/v1/state-versions/{id}/manage
```

Deletes a non-current state version. The current (highest serial) version cannot be deleted.

**Required permission:** `admin` on the workspace.

Returns 204 on success, 409 if attempting to delete the current version.

### Rollback State Version (Terrapod Extension)

```
POST /api/terrapod/v1/state-versions/{id}/actions/rollback
```

Creates a new state version with the content of the specified older version. The new version gets serial = max existing + 1. This is a "copy forward" rollback — no versions are deleted, history is preserved.

**Required permission:** `write` on the workspace.

Returns 201 with the new state version.

### Upload State Manually (Terrapod Extension)

```
POST /api/terrapod/v1/workspaces/{id}/state-versions/actions/upload
```

Upload a raw state JSON file. Serial is auto-assigned (max existing + 1). Useful for state surgery workflows.

**Required permission:** `write` on the workspace.

**Request body:** Raw state JSON (Content-Type: application/json).

Returns 201 with the new state version.

---

## Runs

### Create Run

```
POST /api/v2/runs
```

**Request body:**
```json
{
  "data": {
    "type": "runs",
    "attributes": {
      "message": "Triggered from API",
      "is-destroy": false,
      "auto-apply": false,
      "plan-only": false,
      "target-addrs": ["aws_instance.web"],
      "replace-addrs": [],
      "refresh-only": false,
      "refresh": true,
      "allow-empty-apply": false
    },
    "relationships": {
      "workspace": {
        "data": {
          "id": "ws-abc123",
          "type": "workspaces"
        }
      }
    }
  }
}
```

**Required permission:** `plan` for plan-only runs, `write` for apply runs.

#### Configuration version resolution

The `configuration-version` relationship is optional. Resolution rules:

- **Explicit CV in the request body** → used as-is.
- **No CV + workspace has a VCS connection** → Terrapod fetches the latest commit (or the branch/tag from `vcs-ref`) and creates a fresh CV automatically.
- **No CV + non-VCS workspace** → falls back to the **latest fully-uploaded, non-speculative CV** for the workspace. This is the path the UI's "Queue Plan" button takes.
- **No CV + non-VCS workspace + no upload has ever succeeded** → `422 Unprocessable Entity` with detail `"Workspace has no uploaded configuration. Upload one via 'tofu plan' / 'tofu apply' (CLI), or POST a configuration version + tarball before queueing a run."`. The same response covers the misconfigured-workspace edge case where `vcs_connection_id` is set but `vcs_repo_url` is empty.

The CLI plan/apply flow always supplies a CV (it uploads one first), so it's unaffected by the fallback.

#### Optional Run Attributes

| Attribute | Type | Default | Description |
|---|---|---|---|
| `target-addrs` | array of strings | `[]` | Resource addresses to target (equivalent to `-target` CLI flag) |
| `replace-addrs` | array of strings | `[]` | Resource addresses to force replacement (equivalent to `-replace` CLI flag, plan phase only) |
| `refresh-only` | boolean | `false` | Refresh-only plan — reconcile state without planning changes (equivalent to `-refresh-only`) |
| `refresh` | boolean | `true` | Whether to refresh state before planning. Set to `false` to skip refresh (equivalent to `-refresh=false`) |
| `allow-empty-apply` | boolean | `false` | Allow apply even when the plan has no changes (equivalent to `-allow-empty-apply`) |
| `vcs-ref` | string | `""` | Branch, tag, or SHA to fetch code from instead of the workspace's tracked branch. Only valid on VCS-connected workspaces. **Runs with a non-default ref are always plan-only** — the server enforces this regardless of the `plan-only` attribute value |

### Run Response Attributes (Drift Detection)

Run objects include the following drift detection attributes in responses:

| Attribute | Type | Description |
|---|---|---|
| `is-drift-detection` | boolean | `true` if the run was created by the drift detection scheduler |
| `has-changes` | boolean or null | Whether the plan detected infrastructure changes. `null` if the plan has not completed yet |

Drift detection runs are always plan-only and are not counted in the workspace's normal run queue.

### Run Response Attributes (Resource Profile / OOM)

Run objects include peak resource usage + an abnormal-exit signal so the UI (and external clients) can surface memory pressure without operators having to grep pod logs. All five attributes are `null` / `""` for runs that pre-date the feature or never started a Job.

| Attribute | Type | Source | Description |
|---|---|---|---|
| `resource-cpu` | string | Workspace setting (snapshot) | CPU request applied to the Job (K8s quantity, e.g. `"1"`, `"500m"`). Limit is `2×` this |
| `resource-memory` | string | Workspace setting (snapshot) | Memory request applied to the Job (K8s quantity, e.g. `"2Gi"`). Limit is `2×` this |
| `peak-memory-bytes` | integer or null | Runner (cgroup v2) | Peak resident memory observed during the run — `/sys/fs/cgroup/memory.peak` |
| `peak-cpu-usec` | integer or null | Runner (cgroup v2) | Cumulative CPU time consumed by the run, microseconds — `usage_usec` from `/sys/fs/cgroup/cpu.stat`. **Captured but not surfaced in the UI** — see note below |
| `runner-exit-code` | integer or null | Runner | Runner script's exit code captured at exit. `null` if the trap didn't fire (e.g. SIGKILL) |
| `runner-exit-reason` | string | Listener (K8s) | Raw K8s `container.state.terminated.reason` (e.g. `"OOMKilled"`, `"Error"`, `"Completed"`). `""` if not observed |
| `runner-exit-status` | string | Reconciler (typed bucket) | Stable typed value: `""` (unknown / not yet observed), `"clean"`, `"oom"`, `"killed"`, `"error"`. The UI keys on this; reason is shown for context |

Two independent capture paths feed these fields:

- **Runner path** — `POST /api/terrapod/v1/runs/{run_id}/resource-profile` from the runner's EXIT trap with `peak_memory_bytes` / `peak_cpu_usec` / `exit_code`. Fires for any catchable exit (success, plan errored, OPA failed, SIGTERM during apply).
- **Listener path** — when a Job fails, the listener reads `container.state.terminated.{reason, exit_code}` and POSTs them on the job-status report. The reconciler maps them to `runner_exit_status`.

OOM (`exit 137 + reason "OOMKilled"`) is uncatchable, so the runner path never fires on OOM — the listener path is the only signal. Both paths converge on the same five DB columns; whichever signal arrives wins. `runner-exit-status` is set **only** by the reconciler (single source of truth for typed bucketing) and is what drives the UI's OOM badge + the typed error message ("Runner OOM-killed (peak memory N.NN Gi). Workspace resource_memory is …. Increase resource_memory + retry.").

See [runners.md — Memory Pressure & OOM Visibility](runners.md#memory-pressure--oom-visibility-430) for the operator-facing tuning workflow.

### Show Run

```
GET /api/v2/runs/{run_id}
```

### List Workspace Runs

```
GET /api/v2/workspaces/{id}/runs
```

### Confirm Run (Approve Apply)

```
POST /api/v2/runs/{run_id}/actions/apply
```

**Required permission:** `write` on the workspace.

### Discard Run

```
POST /api/v2/runs/{run_id}/actions/discard
```

**Required permission:** `write` on the workspace.

### Cancel Run

```
POST /api/v2/runs/{run_id}/actions/cancel
```

**Required permission:** `write` on the workspace.

### Retry Run

```
POST /api/terrapod/v1/runs/{run_id}/actions/retry
```

Creates a new run from a terminal run (applied, errored, canceled, discarded) using the same workspace, configuration version, VCS metadata, and settings. Returns a 409 if the run is not in a terminal state.

**Required permission:** `plan` on the workspace (or `write` for apply runs).

### Workspace Events (SSE)

```
GET /api/terrapod/v1/workspaces/{workspace_id}/runs/events
```

Server-Sent Events stream for real-time workspace updates. The stream emits events whenever a run changes state, the workspace is locked/unlocked, workspace settings are updated, or a new state version is created. Used by the web UI workspace detail page for live updates without polling.

**Event types:**

| Event | Trigger |
|---|---|
| `run_status_change` | Run transitions to a new state |
| `workspace_lock_change` | Workspace is locked or unlocked (includes `locked` boolean) |
| `workspace_updated` | Workspace settings are modified |
| `state_version_created` | New state version is uploaded |

The stream sends `: keepalive` comments every ~1 second. Events are JSON-encoded in `data:` fields.

**Required permission:** `read` on the workspace.

### Workspace List Events (SSE) (Terrapod Extension)

```
GET /api/terrapod/v1/workspace-events
```

Server-Sent Events stream for the workspace list page. Emits events whenever any workspace changes (run status, lock, settings, state). The web UI uses this to refresh the workspace list without polling.

**Required permission:** Any authenticated user.

### Plan Details

```
GET /api/terrapod/v1/runs/{run_id}/plan
```

Returns plan metadata and log download URL. When the runner has uploaded a structured plan (`-out=tfplan` → `terraform show -json tfplan`), the response also carries a `json-output` attribute pointing at `/api/v2/plans/{run_id}/json-output`.

### Plan JSON Output

```
GET /api/v2/plans/{plan_id}/json-output
```

Returns the structured JSON representation of the plan, as produced by `terraform show -json tfplan`. Useful for downstream tooling that wants to consume the resource changes without parsing the human-readable log. Responds **302** to a presigned object-storage URL.

The endpoint is mounted at `/api/v2/` because `go-tfe` and Terraform's `cloud` block expect it there. Returns **404** if the runner never uploaded the JSON output (older runs, runs that errored before the plan completed).

### Plan Summary

```
GET /api/v2/plans/{plan_id}/summary
```

Returns the AI-generated plan summary (or failure analysis on errored plans) when the optional `ai_summary` feature is enabled and a summary has been produced for the run. See [docs/ai-plan-summary.md](ai-plan-summary.md) for the operator-side setup.

**Required permission:** `read` on the workspace.

`plan_id` accepts either `plan-{uuid}` (the canonical form, matching what's returned in the `plans` relationship of a Run) or a bare run UUID.

**Responses:**
- **200 OK** — the workspace has a summary row for this run. See response shape below.
- **404 Not Found** — no summary row exists. Either the feature is globally disabled, the workspace opted out (`ai-summary-mode: disabled`), or the summariser hasn't run yet. The UI treats 404 as "no AI surface" and renders nothing.

**Response shape:**

```json
{
  "data": {
    "id": "plan-summary-<uuid>",
    "type": "plan-summaries",
    "attributes": {
      "kind": "plan_summary",
      "status": "ready",
      "description": "Adds a single root-level output named `marker` ...",
      "risk-level": "low",
      "risk-factors": [
        {
          "severity": "low",
          "title": "Output-only addition",
          "detail": "The plan only introduces the `marker` output ...",
          "resource_address": "output.marker"
        }
      ],
      "model": "bedrock/us.anthropic.claude-opus-4-8",
      "input-tokens": 1335,
      "output-tokens": 171,
      "error-message": "",
      "created-at": "2026-06-01T12:00:00Z",
      "updated-at": "2026-06-01T12:00:30Z"
    },
    "relationships": {
      "plan": { "data": { "id": "plan-<uuid>", "type": "plans" } },
      "run":  { "data": { "id": "run-<uuid>",  "type": "runs"  } }
    }
  }
}
```

**Attribute reference:**

| Attribute | Type | Description |
|---|---|---|
| `kind` | string | `"plan_summary"` (successful plan → change description + risk assessment) or `"failure_analysis"` (plan-phase errored → root-cause + suggested fixes). |
| `status` | string | `"pending"` (handler running), `"ready"` (model returned a parseable response), `"skipped"` (workspace disabled or daily budget hit — see `error-message`), or `"errored"` (model call failed — see `error-message`). |
| `description` | string | Markdown body. For `kind=plan_summary`, ~600 words describing the proposed changes. For `kind=failure_analysis`, root-cause explanation. |
| `risk-level` | string | One of `"low"`, `"medium"`, `"high"`, `"critical"`. Reflects blast radius + reversibility, not novelty. |
| `risk-factors` | array of object | Each item carries `severity` (same enum as `risk-level`), `title` (max 120 chars), `detail` (max 600 chars), and optional `resource_address` (terraform address). For `kind=failure_analysis` these are suggested fixes ordered most-likely-to-resolve first. |
| `model` | string | LiteLLM model string used for this summary (e.g. `bedrock/us.anthropic.claude-opus-4-8`). |
| `input-tokens` / `output-tokens` | integer | Telemetry counts reported by the upstream provider. |
| `error-message` | string | Populated only for `status=errored` or `status=skipped`. Empty for `ready`. |

**Real-time updates:** the per-workspace SSE channel (`GET /api/terrapod/v1/workspaces/{id}/runs/events`) emits one of five lifecycle events as the summary progresses (#463):

| Event | Fires when |
|---|---|
| `plan_summary_pending` | Handler dispatched (or operator clicked Regenerate). UI shows a placeholder. |
| `plan_summary_ready` | Initial summary landed; refetch to render. |
| `plan_summary_errored` | Handler/model failure; refetch to render the error. |
| `plan_summary_skipped` | Runner died abnormally / workspace opted out / daily budget hit. |
| `plan_summary_message_posted` | A chat follow-up turn landed (carries `message_id`). Refetch the transcript. |

All five payloads carry `{run_id, workspace_id}` at minimum. The UI re-fetches the summary on any of them. For VCS-driven runs, the per-workspace PR/MR status comment is edited in place to include the summary content when it lands.

### Regenerate Plan Summary

```
POST /api/terrapod/v1/runs/{run_id}/plan-summary/regenerate
```

Re-fires the AI summary handler for a run. Anyone with workspace `read` can regenerate — the call doesn't mutate infrastructure. Bypasses the 5-minute auto-dedup so operator clicks always go through; budget gating still applies handler-side.

**Required permission:** `read` on the workspace.

**Responses:**
- **202 Accepted** — pending row upserted and trigger enqueued. Response shape matches the GET above with `status=pending`.
- **409 Conflict** — run is in a state with no summarisable output yet (still planning, or apply-phase errored).
- **503 Service Unavailable** — AI summary is globally disabled (`api.config.ai_summary.enabled: false`).

### List Plan-Summary Chat Messages

```
GET /api/terrapod/v1/runs/{run_id}/plan-summary/messages
```

Full transcript of the AI plan-summary chat thread in chronological order. The initial structured summary lives on the parent `PlanSummary` row (`description` + `risk-factors`); this endpoint returns ONLY the conversational follow-ups. The UI renders `message[0]` from the parent summary and appends these.

**Required permission:** `read` on the workspace.

**Responses:**
- **200 OK** with an array (possibly empty) of `plan-summary-messages` resources.
- **404 Not Found** — no initial summary exists for this run.
- **409 Conflict** — the initial summary is still `pending` or `errored`. Can't chat against an unready summary.

```json
{
  "data": [
    {
      "id": "plan-summary-message-<uuid>",
      "type": "plan-summary-messages",
      "attributes": {
        "role": "user",
        "content": "How long will the RDS update take?",
        "model": "",
        "input-tokens": 0,
        "output-tokens": 0,
        "error-message": "",
        "created-at": "2026-06-01T12:01:00Z"
      }
    },
    {
      "id": "plan-summary-message-<uuid>",
      "type": "plan-summary-messages",
      "attributes": {
        "role": "assistant",
        "content": "An in-place RDS modify with `apply_immediately = false` typically completes during the next maintenance window…",
        "model": "bedrock/us.anthropic.claude-sonnet-4-6",
        "input-tokens": 14823,
        "output-tokens": 412,
        "error-message": "",
        "created-at": "2026-06-01T12:01:14Z"
      }
    }
  ],
  "meta": { "count": 2 }
}
```

### Post Plan-Summary Chat Message

```
POST /api/terrapod/v1/runs/{run_id}/plan-summary/messages
Content-Type: application/vnd.api+json

{ "data": { "attributes": { "content": "..." } } }
```

Posts a user follow-up + returns the synchronous assistant reply. Authorisation is read-on-workspace — anyone who can see the run can chat in the thread (GitHub PR conversation semantics, not per-user threads).

**Required permission:** `read` on the workspace.

**Responses:**
- **201 Created** — the response body is the assistant turn (same shape as the GET list entries). The persisted user turn is visible via the next GET call.
- **400 Bad Request** — empty body or body > 32 KiB.
- **409 Conflict** — initial summary not `ready`, or this run already has `followup_max_messages_per_run` user turns. The user-turn counter is **server-tracked, not advisory**.
- **429 Too Many Requests** — daily AI token budget exhausted.
- **503 Service Unavailable** — chat globally disabled (`followup_max_messages_per_run: 0`) or workspace opted out.
- **502 Bad Gateway** — model HTTP / parse failure. The user turn is still persisted in the transcript, and a separate errored assistant row is recorded — so a reload shows the failure cleanly.

The model call uses the same cacheable prefix as the initial summary (provider prompt caching serves the prefix hit). See [docs/ai-plan-summary.md#follow-up-chat-463](ai-plan-summary.md#follow-up-chat-463) for the operator-side caps + provider matrix.

### Apply Details

```
GET /api/terrapod/v1/runs/{run_id}/apply
```

Returns apply metadata and log download URL.

---

## Cross-Workspace Remote-State Consumers

Producer-controlled allowlist of workspaces authorized to read this workspace's state via `data "terraform_remote_state"`. Default is empty (not shared) — secure by default. All mutations require **admin/write on the producer** (the state owner). Independent of run triggers — see [the composition guide](remote-state.md).

When a runner-token principal (agent-mode run) hits `/api/v2/workspaces/{id}/current-state-version` or `/api/v2/state-versions/{id}/download` for another workspace, authorization is by this allowlist instead of per-user RBAC. User / API-token principals (CLI, UI, automation) continue through the existing per-user RBAC path.

### List Consumers

```
GET /api/terrapod/v1/workspaces/{id}/remote-state-consumers?filter[remote-state-consumer][type]=outbound
```

`outbound` (default) — workspaces this workspace shares its state to (this workspace is the producer).
`inbound` — workspaces whose state this workspace is authorized to read (this workspace is the consumer).

**Required permission:** `read` on the workspace.

### Authorize a Consumer

```
POST /api/terrapod/v1/workspaces/{producer_id}/remote-state-consumers
```

**Request body:**
```json
{
  "data": {
    "relationships": {
      "consumer": {"data": {"id": "ws-CONSUMER", "type": "workspaces"}}
    }
  }
}
```

**Required permission:** `admin` on the **producer** workspace. A consumer team cannot self-grant.

Errors: `422` self-reference; `409` already authorized; `422` over the per-producer cap.

### Replace Consumer Set (Declarative)

```
PUT /api/terrapod/v1/workspaces/{producer_id}/remote-state-consumers
```

Idempotent declarative replace of the producer's full consumer set in one atomic transaction. Supports the Terrapod provider's set-valued attribute. Body: `{"data": [{"type": "workspaces", "id": "ws-..."}, ...]}`.

**Required permission:** `admin` on the producer.

### Show Consumer Grant

```
GET /api/terrapod/v1/remote-state-consumers/{id}
```

**Required permission:** `read` on the producer.

### Revoke a Consumer Grant

```
DELETE /api/terrapod/v1/remote-state-consumers/{id}
```

**Required permission:** `admin` on the producer. A consumer cannot self-revoke.

### Consumer Grant Response Attributes

| Attribute | Description |
|---|---|
| `producer-workspace-name` | Name of the producer workspace |
| `consumer-workspace-name` | Name of the consumer workspace |
| `created-at` | RFC3339 timestamp the grant was created |
| `created-by` | Identity that created the grant |

Plus relationships `producer` and `consumer` (both `workspaces`-typed).

---

## Run Triggers

Run triggers create cross-workspace dependency chains. When a source workspace completes an apply, all downstream workspaces with an inbound trigger automatically get a new run queued.

### Create Run Trigger

```
POST /api/terrapod/v1/workspaces/{id}/run-triggers
```

**Request body:**
```json
{
  "data": {
    "relationships": {
      "sourceable": {
        "data": {
          "id": "ws-source-workspace-id",
          "type": "workspaces"
        }
      }
    }
  }
}
```

**Required permission:** `admin` on the destination workspace.

**Validation:**
- Source and destination must be different workspaces
- No duplicate triggers for the same pair
- Maximum 20 source workspaces per destination

**Example:**
```bash
curl -s \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -X POST \
  https://terrapod.example.com/api/terrapod/v1/workspaces/ws-abc123/run-triggers \
  -d '{
    "data": {
      "relationships": {
        "sourceable": {
          "data": {"id": "ws-def456", "type": "workspaces"}
        }
      }
    }
  }'
```

### List Run Triggers

```
GET /api/terrapod/v1/workspaces/{id}/run-triggers?filter[run-trigger][type]=inbound|outbound
```

- `inbound`: triggers where this workspace is the destination (what triggers runs here?)
- `outbound`: triggers where this workspace is the source (what does my apply trigger?)

The `filter[run-trigger][type]` parameter is required (422 if missing).

**Required permission:** `read` on the workspace.

**Example:**
```bash
curl -s \
  -H "Authorization: Bearer $TOKEN" \
  "https://terrapod.example.com/api/terrapod/v1/workspaces/ws-abc123/run-triggers?filter[run-trigger][type]=inbound"
```

### Show Run Trigger

```
GET /api/terrapod/v1/run-triggers/{id}
```

**Required permission:** `read` on the destination workspace.

### Delete Run Trigger

```
DELETE /api/terrapod/v1/run-triggers/{id}
```

**Required permission:** `admin` on the destination workspace.

**Example:**
```bash
curl -s \
  -H "Authorization: Bearer $TOKEN" \
  -X DELETE \
  https://terrapod.example.com/api/terrapod/v1/run-triggers/rt-abc123
```

---

## Configuration Versions

A workspace's uploaded source archives are also browsable from the UI — workspace detail → Configurations tab. The list highlights the current configuration with a `current` badge and supports download + side-by-side diff between any two versions.

![Workspace Configurations](images/workspace-configurations.png)

### Create Configuration Version

```
POST /api/v2/workspaces/{id}/configuration-versions
```

**Request body:**
```json
{
  "data": {
    "type": "configuration-versions",
    "attributes": {
      "auto-queue-runs": true
    }
  }
}
```

**Response includes:** `upload-url` attribute with a presigned URL for uploading the tarball.

**Required permission:** `write` on the workspace.

### Upload Configuration

```
PUT <upload-url>
Content-Type: application/octet-stream

<tarball bytes>
```

No auth required (presigned URL).

### Show Configuration Version

```
GET /api/v2/configuration-versions/{cv_id}
```

Returns a single CV's metadata.

### List Configuration Versions

```
GET /api/v2/workspaces/{id}/configuration-versions
```

Newest first. Supports `page[size]` (default 20, max 100) and `page[number]`.

**Response includes** `meta.current-id` — the CV id consumed by the most recent successful apply, or `null` if none. The UI uses this to badge the current row.

**Required permission:** `read` on the workspace.

### Download Configuration Version (Terrapod extension)

```
GET /api/terrapod/v1/configuration-versions/{cv_id}/download
```

Streams the tarball bytes back as `application/x-tar` with a `Content-Disposition: attachment` header. Bearer auth.

**Status codes:**
- 200 — streaming tarball
- 404 — CV doesn't exist or caller lacks `read`
- 409 — CV exists but bytes haven't been uploaded yet
- 410 — CV row exists but tarball was swept by retention

**Required permission:** `read` on the owning workspace.

### Mint Download Ticket (Terrapod extension)

```
POST /api/terrapod/v1/configuration-versions/{cv_id}/download-ticket
```

Mints a short-lived, single-resource HMAC ticket the browser can paste into a plain `<a href>` to stream a download natively to the user's save dialog. **Opt-in** — the default download path above is the simple Bearer-auth flow; tickets exist because plain navigation can't carry an `Authorization` header.

**Request body** (optional):
```json
{"data": {"attributes": {"ttl-seconds": 300}}}
```

TTL defaults to 300 s, hard-capped at 1800 s. Negative or zero values fall back to the default.

**Response:**
```json
{
  "data": {
    "type": "download-tickets",
    "attributes": {
      "ticket": "dlticket:cv:{uuid}:...",
      "url": "/api/terrapod/v1/configuration-versions/download-by-ticket/dlticket:cv:...",
      "expires-at": "2026-05-07T12:34:56Z"
    }
  }
}
```

**Required permission:** `read` on the owning workspace (same gate as the direct download).

### Download by Ticket (Terrapod extension)

```
GET /api/terrapod/v1/configuration-versions/download-by-ticket/{ticket}
```

Streams the tarball — no `Authorization` header. The ticket is the auth: HMAC-SHA256 over the resource id, expiry, and minter email, signed with the same key class as runner tokens. Single-resource (a CV-X ticket cannot fetch CV-Y); short TTL bounds replay.

**Status codes:**
- 200 — streaming tarball
- 401 — malformed, expired, or bad-signature ticket
- 410 — CV bytes swept by retention since mint

### Diff Configuration Versions (Terrapod extension)

```
POST /api/terrapod/v1/configuration-versions/diff
```

Compares two CVs in the same workspace and returns per-file unified diffs.

**Request body:**
```json
{
  "data": {
    "attributes": {
      "from-id": "cv-...",
      "to-id":   "cv-..."
    }
  }
}
```

**Response shape:**
```json
{
  "data": {
    "type": "configuration-version-diffs",
    "attributes": {
      "from-id": "cv-...",
      "to-id":   "cv-...",
      "files": [
        {"path": "main.tf", "type": "modified", "diff": "@@ ... @@"},
        {"path": "vars.tf", "type": "added",    "diff": "..."},
        {"path": "old.tf",  "type": "removed",  "diff": "..."},
        {"path": "logo.png","type": "binary-changed"}
      ],
      "oversized": ["modules/big.zip"],
      "total-files-changed": 4
    }
  }
}
```

**Limits:** per-file 1 MiB (oversized files report only their path), per-pair 32 MiB total (refused with 413 if exceeded). Binary files (NUL byte in first 8 KiB) report only `binary-changed`.

**Status codes:**
- 200 — diff returned
- 404 — either CV missing or caller lacks `read`
- 409 — either CV not yet uploaded
- 410 — bytes swept by retention
- 413 — combined size exceeds the per-pair cap
- 422 — missing ids or cross-workspace request

**Required permission:** `read` on the workspace (both CVs must belong to the same workspace).

---

## Labels

Read-only labels browser (Terrapod extension). All endpoints are RBAC-filtered: results only include labels carried by entities the caller has at least `read` on for that entity's permission model. Editing labels still happens on each entity's own edit page — there is no labels-admin surface.

### List Label Keys

```
GET /api/terrapod/v1/labels
```

Returns all label keys in use across readable workspaces, modules, providers, and pools, with per-type counts.

**Response shape:**
```json
{
  "data": [
    {"key": "team", "value-count": 4, "by-type": {"workspaces": 12, "modules": 2, "providers": 0, "pools": 1}}
  ]
}
```

### List Values for a Key

```
GET /api/terrapod/v1/labels/{key}
```

Returns distinct values for `key`, each with per-type counts. Empty `data` is a valid response.

### List Entities for a Label

```
GET /api/terrapod/v1/labels/{key}/{value}
```

Returns entities tagged with exactly `key=value`, grouped by type.

**Response shape:**
```json
{
  "data": {
    "workspaces": [{"id": "ws-...", "name": "..."}],
    "modules":    [{"id": "mod-...", "name": "...", "provider": "..."}],
    "providers":  [{"id": "prov-...", "namespace": "...", "name": "..."}],
    "pools":      [{"id": "pool-...", "name": "..."}]
  }
}
```

---

## Variables

### List Workspace Variables

```
GET /api/v2/workspaces/{id}/vars
```

**Required permission:** `read` on the workspace. Sensitive values are never returned.

### Create Variable

```
POST /api/v2/workspaces/{id}/vars
```

**Request body:**
```json
{
  "data": {
    "type": "vars",
    "attributes": {
      "key": "AWS_REGION",
      "value": "eu-west-1",
      "category": "env",
      "sensitive": false,
      "description": "AWS region for provider"
    }
  }
}
```

`category` is either `terraform` (injected as `TF_VAR_{key}`) or `env` (injected as raw env var).

**Required permission:** `write` on the workspace.

### Update Variable

```
PATCH /api/v2/workspaces/{id}/vars/{var_id}
```

### Delete Variable

```
DELETE /api/v2/workspaces/{id}/vars/{var_id}
```

**Required permission:** `write` on the workspace.

---

## Variable Sets

### List Variable Sets

```
GET /api/v2/organizations/default/varsets
```

### Create Variable Set

```
POST /api/v2/organizations/default/varsets
```

**Required permission:** Platform `admin`.

### Variable Set Variables

```
GET    /api/v2/varsets/{varset_id}/relationships/vars
POST   /api/v2/varsets/{varset_id}/relationships/vars
PATCH  /api/v2/varsets/{varset_id}/relationships/vars/{var_id}
DELETE /api/v2/varsets/{varset_id}/relationships/vars/{var_id}
```

### Variable Set Workspace Assignments

```
POST   /api/v2/varsets/{varset_id}/relationships/workspaces
DELETE /api/v2/varsets/{varset_id}/relationships/workspaces
```

---

## Registry -- Modules

### CLI Protocol (for terraform init)

```
GET /api/v2/registry/modules/{namespace}/{name}/{provider}/versions
GET /api/v2/registry/modules/{namespace}/{name}/{provider}/{version}/download
```

### TFE V2 Management API

```
GET  /api/terrapod/v1/registry-modules
POST /api/terrapod/v1/registry-modules
GET  /api/terrapod/v1/registry-modules/private/default/{name}/{provider}
DELETE /api/terrapod/v1/registry-modules/private/default/{name}/{provider}
POST /api/terrapod/v1/registry-modules/private/default/{name}/{provider}/versions
DELETE /api/terrapod/v1/registry-modules/private/default/{name}/{provider}/versions/{version}
```

### Update Module

```
PATCH /api/terrapod/v1/registry-modules/private/default/{name}/{provider}
```

**Required permission:** `admin` on the module.

**Self-lockout protection:** If the request changes `labels` and the new labels would reduce the caller's own access level, the API returns **409 Conflict**. Re-submit with `"force": true` in the attributes to confirm.

### Module Permissions Block

All module responses (show and list) include a `permissions` object:

```json
{
  "permissions": {
    "can-update": true,
    "can-destroy": true,
    "can-create-version": true
  }
}
```

### Version Upload

Create a version, then upload the tarball to the presigned URL returned in the response.

### Workspace Links (Module Impact Analysis)

```
GET    /api/terrapod/v1/registry-modules/private/default/{name}/{provider}/workspace-links
POST   /api/terrapod/v1/registry-modules/private/default/{name}/{provider}/workspace-links
DELETE /api/terrapod/v1/registry-modules/private/default/{name}/{provider}/workspace-links/{link_id}
```

**Required permission:** `admin` on the module (create/delete), `read` on the module (list).

**Terraform provider resource:** `terrapod_module_workspace_link`

---

## Registry -- Providers

### CLI Protocol (for terraform init)

```
GET /api/v2/registry/providers/{namespace}/{type}/versions
GET /api/v2/registry/providers/{namespace}/{type}/{version}/download/{os}/{arch}
```

### TFE V2 Management API

```
GET  /api/terrapod/v1/registry-providers
POST /api/terrapod/v1/registry-providers
GET  /api/terrapod/v1/registry-providers/private/default/{name}
DELETE /api/terrapod/v1/registry-providers/private/default/{name}
POST /api/terrapod/v1/registry-providers/private/default/{name}/versions
GET  /api/terrapod/v1/registry-providers/private/default/{name}/versions
DELETE /api/terrapod/v1/registry-providers/private/default/{name}/versions/{version}
POST /api/terrapod/v1/registry-providers/private/default/{name}/versions/{version}/platforms
```

### Update Provider

```
PATCH /api/terrapod/v1/registry-providers/private/default/{name}
```

**Required permission:** `admin` on the provider.

**Self-lockout protection:** If the request changes `labels` and the new labels would reduce the caller's own access level, the API returns **409 Conflict**. Re-submit with `"force": true` in the attributes to confirm.

### Provider Permissions Block

All provider responses (show and list) include a `permissions` object:

```json
{
  "permissions": {
    "can-update": true,
    "can-destroy": true,
    "can-create-version": true
  }
}
```

### GPG Keys

```
GET    /api/terrapod/v1/gpg-keys
POST   /api/terrapod/v1/gpg-keys
GET    /api/terrapod/v1/gpg-keys/{namespace}/{key_id}
DELETE /api/terrapod/v1/gpg-keys/{namespace}/{key_id}
```

---

## Agent Pools

### List Pools

```
GET /api/terrapod/v1/agent-pools
```

### Create Pool

```
POST /api/terrapod/v1/agent-pools
```

**Request body:**
```json
{
  "data": {
    "type": "agent-pools",
    "attributes": {
      "name": "aws-prod",
      "description": "Production AWS runners"
    }
  }
}
```

**Required permission:** Platform `admin`.

### Show Pool

```
GET /api/terrapod/v1/agent-pools/{id}
```

### Delete Pool

```
DELETE /api/terrapod/v1/agent-pools/{id}
```

### Pool Tokens

```
POST /api/terrapod/v1/agent-pools/{id}/authentication-tokens
GET  /api/terrapod/v1/agent-pools/{id}/authentication-tokens
```

### Listener Join

```
POST /api/terrapod/v1/agent-pools/join
```

Registers a listener using a join token. The token identifies the pool — no pool ID needed in the URL. No Bearer auth required; the join token in the body IS the credential.

**Request body:**
```json
{
  "join_token": "<raw-token>",
  "name": "my-listener"
}
```

**Response:** listener ID, pool ID, X.509 certificate, private key, CA certificate.

If a listener with the same name already exists, its certificate is reissued (handles pod restarts).

**Legacy endpoint** (still supported):
```
POST /api/terrapod/v1/agent-pools/{pool_id}/listeners/join
```

### Listener Heartbeat

```
POST /api/terrapod/v1/listeners/{id}/heartbeat
```

### Listener Certificate Renewal

```
POST /api/terrapod/v1/listeners/{id}/renew
```

### Listener Run Polling

```
GET /api/terrapod/v1/listeners/{id}/runs/next
```

Returns the next queued run for this listener.

### Listener Runner Token

```
POST /api/terrapod/v1/listeners/{id}/runs/{run_id}/runner-token
```

Generates a short-lived HMAC-signed runner token scoped to the specified run. Called by the listener after claiming a run.

**Request body (optional):**
```json
{
  "ttl": 3600
}
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `ttl` | integer | `runners.tokenTTLSeconds` (default 3600) | Requested token lifetime in seconds. Clamped to `runners.maxTokenTTLSeconds` (default 7200) |

**Response:**
```json
{
  "token": "runtok:{run_id}:{ttl}:{timestamp}:{hmac_sig}",
  "expires_in": 3600
}
```

**Auth:** Listener certificate.

### Listener Status Update

```
PATCH /api/terrapod/v1/listeners/{id}/runs/{run_id}
```

Reports run status changes (planning, planned, applying, applied, errored).

---

## VCS Connections

### List Connections

```
GET /api/terrapod/v1/vcs-connections
```

### Create Connection

```
POST /api/terrapod/v1/vcs-connections
```

**GitHub example:**
```json
{
  "data": {
    "type": "vcs-connections",
    "attributes": {
      "name": "my-github",
      "provider": "github",
      "github-app-id": 12345,
      "github-installation-id": 112887490,
      "github-account-login": "my-org",
      "github-account-type": "Organization",
      "private-key": "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
    }
  }
}
```

**GitLab example:**
```json
{
  "data": {
    "type": "vcs-connections",
    "attributes": {
      "name": "my-gitlab",
      "provider": "gitlab",
      "token": "glpat-xxxxxxxxxxxxxxxxxxxx"
    }
  }
}
```

**Required permission:** Platform `admin`.

### Show Connection

```
GET /api/terrapod/v1/vcs-connections/{id}
```

### Update Connection

```
PATCH /api/terrapod/v1/vcs-connections/{id}
```

Partial update — only the attributes you include are changed. Notes:

- `provider` is **immutable**. A different provider is a different connection; delete and recreate to change it (sending a different `provider` returns `422`).
- Credentials (`private-key` for GitHub, `token` for GitLab) are **write-only**: they are never returned, and are only rotated when you send a non-empty value. Omit them to change the name/server-url/status without touching the stored credential.
- Editable: `name`, `server-url`, `status` (`active`/`disabled`), and the GitHub App identifiers (`github-app-id`, `github-installation-id`, `github-account-login`, `github-account-type`). Changing `github-installation-id` to one already used by another connection returns `422`.

```json
{
  "data": {
    "type": "vcs-connections",
    "attributes": { "name": "renamed", "server-url": "https://github.example.com/api/v3" }
  }
}
```

**Required permission:** Platform `admin`.

### Delete Connection

```
DELETE /api/terrapod/v1/vcs-connections/{id}
```

---

## Autodiscovery Rules

Connection-scoped rules that auto-create workspaces when a PR or default-branch push touches a path matching `pattern`. See [Autodiscovery](autodiscovery.md) for the full feature doc.

All endpoints require `admin` role.

### List Rules

```
GET /api/terrapod/v1/autodiscovery-rules
```

### Create Rule

```
POST /api/terrapod/v1/autodiscovery-rules
```

**Request body** (every attribute except `name`, `vcs-connection-id`, `repo-url`, `pattern` is optional):

```json
{
  "data": {
    "type": "autodiscovery-rules",
    "attributes": {
      "name": "monorepo",
      "vcs-connection-id": "vcs-019e0e7b-...",
      "repo-url": "https://github.com/myorg/monorepo",
      "branch": "main",
      "pattern": "accounts/*/**/*.tf",
      "ignore-patterns": ["modules/**"],
      "name-template": "ws-{path}",
      "enabled": true,
      "execution-mode": "agent",
      "execution-backend": "tofu",
      "agent-pool-id": "apool-019e01db-...",
      "terraform-version": "1.12",
      "resource-cpu": "1",
      "resource-memory": "2Gi",
      "auto-apply": false,
      "on-directory-delete": "flag",
      "labels": {"managed-by": "monorepo-autodiscover"},
      "owner-email": "platform@example.com"
    }
  }
}
```

Returns `201` with the created rule, or `409` if a rule with that name already exists for the connection.

> **Reserved label keys:** `labels` is validated like any other label write — reserved keys (`status`, `pool`, `mode`, `backend`, `owner`, `drift`, `version`, `vcs`, `locked`, `branch`) are rejected with `422`. This is enforced at rule create/update so the rule can't materialise workspaces that later become uneditable.

### Show Rule

```
GET /api/terrapod/v1/autodiscovery-rules/{id}
```

### Update Rule

```
PATCH /api/terrapod/v1/autodiscovery-rules/{id}
```

Same body shape as create; only the attributes you include are updated.

### Delete Rule

```
DELETE /api/terrapod/v1/autodiscovery-rules/{id}
```

Workspaces auto-created by this rule keep working — their `autodiscovery-rule-id` foreign key is set to NULL.

### Preview (dry-run)

Walk the repo and return exactly which workspaces a rule *would* create against the current state of the tracked branch, with no side effects. Used by the admin UI's "Preview" modal.

```
GET  /api/terrapod/v1/autodiscovery-rules/{id}/preview      # preview a saved rule
POST /api/terrapod/v1/autodiscovery-rules/preview           # preview an unsaved rule (same attributes body as Create)
```

Each entry reports `workspace_name`, `working_directory`, `collision` (the row would no-op — a workspace is already bound to that directory or the derived name is taken), and `existing_autodiscovered` (the no-op is a reuse of a workspace this same rule already materialised). Returns `413` if the VCS provider truncated the repo tree (repo too large to scan in one pass).

### Scan (on-demand materialise)

```
POST /api/terrapod/v1/autodiscovery-rules/{id}/scan
```

Runs the same walk as Preview but actually creates the workspaces (idempotent, collision-safe). Force-enables the rule for the duration of the call so an explicit operator action doesn't silently no-op on a disabled rule. New workspaces are seeded with the tracked-branch HEAD as their last-seen commit, so the first real plan+apply fires when the branch next advances (e.g. the PR merge) — not immediately against a branch where the directory doesn't exist yet.

### Rule templating (run tasks / notifications / var files)

`POST`/`PATCH` rule bodies accept three additional attributes that are **materialised onto every workspace the rule creates**, so autodiscovered workspaces are fully configured at creation:

- `var-files` — list of var-file paths.
- `run-task-templates` — list of run-task specs (same shape as the bulk-update `run-tasks`, below): `{name, url, hmac-key?, stage, enforcement-level?, enabled?}`.
- `notification-templates` — list of notification specs: `{name, destination-type, url?, token?, triggers?, email-addresses?, enabled?}`.

These use the **identical spec shape** as the bulk-update endpoint, so a run task defined once can be applied to existing workspaces (bulk-update) *and* auto-applied to future ones (this template).

---

## Bulk Workspace Operations

Terrapod-native, **admin only**. Server-side selection + atomic fleet updates (#318).

### Search

```
POST /api/terrapod/v1/workspaces/actions/search
```

Resolve a structured filter to the matching workspaces — no side effects (the discovery half of the bulk workflow).

```json
{ "filter": {
    "labels": {"team": "foundations"},
    "name-prefix": "terrapod-testing-",
    "execution-backend": "terraform",
    "agent-pool-id": "apool-...", "vcs-connection-id": "vcs-...",
    "owner-email": "...", "drift-status": "...", "locked": false, "has-vcs": true,
    "workspace-ids": ["ws-..."],
    "all": false } }
```

Dimensions are **AND-combined** (narrower = safer). An empty/omitted `filter` is a `422` (typo guard); matching the whole fleet requires explicit `"all": true`. Returns `{matched, workspaces:[...]}`.

### Bulk Update

```
POST /api/terrapod/v1/workspaces/actions/bulk-update
```

Apply `update` to every workspace matching `filter`, in a **single all-or-nothing transaction**.

```json
{ "filter": { "labels": {"team": "foundations"} },
  "update": {
    "terraform-version": "1.12",
    "execution-backend": "tofu",
    "auto-apply": false,
    "agent-pool-id": "apool-...",
    "resource-cpu": "1", "resource-memory": "2Gi",
    "var-files": ["envs/prod.tfvars"],
    "labels": {"reviewed": "2026-q2"},
    "run-tasks": [
      { "name": "opa-policy-check", "url": "http://opa:8080/webhook",
        "hmac-key": "secret", "stage": "post_plan", "enforcement-level": "mandatory" }
    ],
    "notification-configurations": [
      { "name": "slack-prod", "destination-type": "slack",
        "url": "https://hooks.slack.com/...", "triggers": ["run:errored"] }
    ]
  },
  "dry_run": true }
```

Semantics:

- **Validated once up front** — field enums, `labels` reserved-key check, run-task/notification specs, and `agent-pool-id` existence + caller pool-`write` RBAC. Any error ⇒ `422`, **zero mutation**.
- `run-tasks` / `notification-configurations` **upsert by `(workspace, name)`**: created if absent, updated in place if present (so re-running with a changed `url` rotates it across the fleet).
- **All-or-nothing**: the whole batch commits or nothing does. `dry_run` (default `true`, not enforced) runs the identical code path and rolls back — the preview is exactly what apply would do, with provably zero side effects.
- **Triggers no runs** — pure config write; the change lands on each workspace's next normal run. Reversible (it only writes settings rows).
- Per-workspace audit entries.

Response: dry-run `{dry_run:true, matched, would_change:[{id,name,diff}], unchanged}`; apply `{dry_run:false, matched, applied, changes, unchanged, errors:[]}`; any failure ⇒ `409`/`422` and **nothing applied**.

---

## VCS Events (Webhooks)

### GitHub Webhook Receiver

```
POST /api/terrapod/v1/vcs-events/github
```

Validates HMAC-SHA256 signature and triggers an immediate poll cycle. The webhook secret must match `TERRAPOD_VCS__GITHUB__WEBHOOK_SECRET`.

---

## Roles

### List Roles

```
GET /api/terrapod/v1/roles
```

Returns built-in and custom roles.

**Required permission:** Platform `admin` or `audit`.

### Create Role

```
POST /api/terrapod/v1/roles
```

**Request body:**
```json
{
  "data": {
    "type": "roles",
    "attributes": {
      "name": "developer",
      "description": "Development workspace access",
      "workspace-permission": "write",
      "allow-labels": {"env": "dev"},
      "allow-names": [],
      "deny-labels": {},
      "deny-names": []
    }
  }
}
```

**Required permission:** Platform `admin`.

### Show Role

```
GET /api/terrapod/v1/roles/{name}
```

### Update Role

```
PATCH /api/terrapod/v1/roles/{name}
```

### Delete Role

```
DELETE /api/terrapod/v1/roles/{name}
```

Built-in roles cannot be deleted.

---

## Role Assignments

### List Assignments

```
GET /api/terrapod/v1/role-assignments
```

**Required permission:** Platform `admin` or `audit`.

### Set Roles for User

```
PUT /api/terrapod/v1/role-assignments
```

**Request body:**
```json
{
  "data": {
    "type": "role-assignments",
    "attributes": {
      "provider-name": "local",
      "email": "alice@example.com",
      "roles": ["developer", "sre-reader"]
    }
  }
}
```

**Required permission:** Platform `admin`.

### Remove Single Assignment

```
DELETE /api/terrapod/v1/role-assignments/{provider}/{email}/{role}
```

---

## Authentication Tokens

### Create Token

```
POST /api/terrapod/v1/users/{user_id}/authentication-tokens
```

### List Tokens

```
GET /api/terrapod/v1/users/{user_id}/authentication-tokens
```

### Show Token

```
GET /api/terrapod/v1/authentication-tokens/{id}
```

### Delete Token

```
DELETE /api/terrapod/v1/authentication-tokens/{id}
```

---

## Run Artifacts (Runner)

Authenticated endpoints for runner Jobs to download inputs and upload outputs. All endpoints require a runner token (`Authorization: Bearer runtok:...`) scoped to the specified `run_id`.

### Download Config Archive

```
GET /api/terrapod/v1/runs/{run_id}/artifacts/config
```

Returns 302 redirect to presigned storage URL for the configuration tarball.

### Download State

```
GET /api/terrapod/v1/runs/{run_id}/artifacts/state
```

Returns 302 redirect to presigned storage URL for the current workspace state.

### Download Plan File

```
GET /api/terrapod/v1/runs/{run_id}/artifacts/plan-file
```

Returns 302 redirect to presigned storage URL for the plan binary file.

### Upload Plan Log

```
PUT /api/terrapod/v1/runs/{run_id}/artifacts/plan-log
Content-Type: application/octet-stream
```

Upload raw plan log bytes. Returns 204 on success.

### Upload Plan File

```
PUT /api/terrapod/v1/runs/{run_id}/artifacts/plan-file
Content-Type: application/octet-stream
```

Upload plan binary file. Returns 204 on success.

### Upload Apply Log

```
PUT /api/terrapod/v1/runs/{run_id}/artifacts/apply-log
Content-Type: application/octet-stream
```

Upload raw apply log bytes. Returns 204 on success.

### Upload State

```
PUT /api/terrapod/v1/runs/{run_id}/artifacts/state
Content-Type: application/octet-stream
```

Upload new state after apply. Returns 204 on success.

### Download Plan Artifacts

```
GET /api/terrapod/v1/runs/{run_id}/artifacts/plan-artifacts
```

Returns 302 redirect to presigned storage URL for the plan-phase workspace-diff tarball. Used by the apply phase to restore files generated during plan (e.g. `data.archive_file` outputs, `null_resource` local-exec scratch) into the apply Job's fresh workspace. Returns 404 if the run was produced by a pre-v0.34.0 runner (no plan-artifacts uploaded). The apply phase tolerates 404 — it logs an error and proceeds.

### Upload Plan Artifacts

```
PUT /api/terrapod/v1/runs/{run_id}/artifacts/plan-artifacts
Content-Type: application/x-tar
Content-Length: <bytes>
```

Upload the plan-phase workspace-diff tarball. The body is the diff (`post_plan - post_init`) of files plan created, excluding `tfplan`, `.terraform.lock.hcl`, and `.terraform/terraform.tfstate` (handled by other endpoints). An empty tar is always uploaded — even when plan generated no new files — so the apply-side contract is "404 means upload genuinely failed", not "no new files".

The body is streamed to an ephemeral tempfile on the API pod's PVC and forwarded to object storage; nothing is fully buffered in RAM. Maximum tarball size is `runners.planArtifactsMaxBytes` (default 256 MiB; minimum 10240 bytes). Both the `Content-Length` header and the streamed length are enforced — oversize uploads receive HTTP 413.

Returns 204 on success.

### Record Resource Profile

```
POST /api/terrapod/v1/runs/{run_id}/resource-profile
Content-Type: application/json
```

Called by the runner entrypoint at exit (EXIT trap, fires on every catchable termination — clean success, plan errored, OPA failed, SIGTERM during apply). Captures cgroup-v2 peak memory + cumulative CPU + the runner script's exit code.

Body (all fields optional — the runner sends whatever it could read):

```json
{
  "peak_memory_bytes": 1500000000,
  "peak_cpu_usec": 42000000,
  "exit_code": 0
}
```

- `peak_memory_bytes` — from `/sys/fs/cgroup/memory.peak`
- `peak_cpu_usec` — `usage_usec` from `/sys/fs/cgroup/cpu.stat`
- `exit_code` — script's actual exit status

Negative values, non-integers, or booleans return `400`. Missing fields are not clobbered (existing values preserved).

Note: **SIGKILL is uncatchable**, so this endpoint never fires on OOM-killed runs. Those are covered by the listener's K8s-terminated-state report on the job-status path; `runner-exit-status` ends up `"oom"` either way. See [Run Response Attributes (Resource Profile / OOM)](#run-response-attributes-resource-profile--oom) for the full signal flow.

Returns 204 on success.

---

## Binary Cache

### Download Binary

```
GET /api/terrapod/v1/binary-cache/{tool}/{version}/{os}/{arch}
```

Returns a 302 redirect to a presigned URL for the binary. `tool` is `terraform` or `tofu`.

**Required:** Authentication (runner token, API token, or session).

### List Cached Binaries (Admin)

```
GET /api/terrapod/v1/admin/binary-cache
```

### Warm Cache (Admin)

```
POST /api/terrapod/v1/admin/binary-cache/warm
```

Pre-cache a specific tool version.

### Purge Cache (Admin)

```
DELETE /api/terrapod/v1/admin/binary-cache/{tool}/{version}
```

---

## Agent Pool Events (SSE)

```
GET /api/terrapod/v1/agent-pools/{pool_id}/events
```

Server-Sent Events stream for real-time agent pool updates. Emits events when listeners heartbeat or join the pool. Used by the agent pool detail page for live listener status updates.

**Event types:**

| Event | Trigger |
|---|---|
| `listener_heartbeat` | A listener sends its periodic heartbeat |
| `listener_joined` | A new listener joins the pool |

**Required permission:** Platform `admin` or `audit`.

---

## Provider Cache (Network Mirror)

All provider mirror endpoints require authentication (runner token, API token, or session).

### Provider Version Index

```
GET /v1/providers/{hostname}/{namespace}/{type}/index.json
```

### Provider Version Details

```
GET /v1/providers/{hostname}/{namespace}/{type}/{version}.json
```

Returns platform-specific download URLs with `zh:` (zip hash) checksums.

---

## Auth Endpoints

### List Auth Providers

```
GET /api/terrapod/v1/auth/providers
```

### Authorize (Start Login)

```
GET /api/terrapod/v1/auth/authorize
```

### Callback (IDP Return)

```
GET /api/terrapod/v1/auth/callback
POST /api/terrapod/v1/auth/callback
```

### Active Sessions

```
GET /api/terrapod/v1/auth/sessions
```

### Logout

```
POST /api/terrapod/v1/auth/logout
```

---

## OAuth (Terraform Login)

### Authorize

```
GET /oauth/authorize
```

### Token Exchange

```
POST /oauth/token
```

---

## Audit Log

Immutable record of API requests. Requires `admin` or `audit` role.

### List Audit Log Entries

```
GET /api/terrapod/v1/admin/audit-log
```

**Query Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `filter[actor]` | string | Filter by actor email |
| `filter[resource-type]` | string | Filter by resource type (e.g. `workspaces`, `runs`) |
| `filter[action]` | string | Filter by HTTP method (GET, POST, PATCH, DELETE) |
| `filter[since]` | datetime | Only entries after this timestamp (RFC3339) |
| `filter[until]` | datetime | Only entries before this timestamp (RFC3339) |
| `page[number]` | integer | Page number (default: 1) |
| `page[size]` | integer | Page size (default: 20, max: 100) |

**Response:** JSON:API list of `audit-log-entries` with pagination metadata.

**Example:**

```zsh
curl "https://terrapod.example.com/api/terrapod/v1/admin/audit-log?filter[actor]=admin@example.com&page[size]=10" \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

---

## Users

User management endpoints. List and show require `admin` or `audit` role. Update and delete require `admin` role.

### List Users

```
GET /api/terrapod/v1/users
```

**Query Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `filter[email]` | string | Filter by email (case-insensitive substring match) |
| `page[number]` | integer | Page number (default: 1) |
| `page[size]` | integer | Page size (default: 20, max: 100) |

### Show User

```
GET /api/terrapod/v1/users/{email}
```

### Update User

```
PATCH /api/terrapod/v1/users/{email}
```

**Updatable attributes:** `is-active`, `display-name`.

When `is-active` is set to `false`, all sessions for that user are revoked immediately.

### Delete User

```
DELETE /api/terrapod/v1/users/{email}
```

Cascades: revokes all sessions, deletes all role assignments.

---

## Notification Configurations

Workspace-scoped notifications that fire on run lifecycle events. Three destination types: **generic** (webhook with HMAC-SHA512 signing), **slack** (Block Kit formatted), and **email** (SMTP).

### Create Notification Configuration

```
POST /api/terrapod/v1/workspaces/{id}/notification-configurations
```

**Request body:**
```json
{
  "data": {
    "type": "notification-configurations",
    "attributes": {
      "name": "deploy-alerts",
      "destination-type": "generic",
      "url": "https://example.com/webhook",
      "token": "my-hmac-secret",
      "enabled": true,
      "triggers": ["run:completed", "run:errored"]
    }
  }
}
```

**Destination types:**

| Type | Required fields | Optional |
|---|---|---|
| `generic` | `url` | `token` (HMAC-SHA512 signing) |
| `slack` | `url` (Slack webhook URL) | — |
| `email` | `email-addresses` (list) | — |

**Valid triggers:** `run:created`, `run:planning`, `run:needs_attention`, `run:planned`, `run:applying`, `run:completed`, `run:errored`, `run:drift_detected`

**Required permission:** `admin` on the workspace.

### List Notification Configurations

```
GET /api/terrapod/v1/workspaces/{id}/notification-configurations
```

**Required permission:** `read` on the workspace.

### Show Notification Configuration

```
GET /api/terrapod/v1/notification-configurations/{id}
```

**Required permission:** `read` on the associated workspace.

### Update Notification Configuration

```
PATCH /api/terrapod/v1/notification-configurations/{id}
```

Same body format as create. Only include attributes to change. Token is never returned in responses — only `has-token: true/false`.

**Required permission:** `admin` on the workspace.

### Delete Notification Configuration

```
DELETE /api/terrapod/v1/notification-configurations/{id}
```

**Required permission:** `admin` on the workspace.

### Verify Notification Configuration

```
POST /api/terrapod/v1/notification-configurations/{id}/actions/verify
```

Sends a test payload to the configured destination and returns the delivery response.

**Required permission:** `admin` on the workspace.

---

## Policy Sets

OPA policy-as-code enforcement. Policy sets and their policies are admin-managed; per-run policy evaluations are readable by anyone with read on the run's workspace. See [policies.md](policies.md) for the Rego authoring contract.

### List Policy Sets

```
GET /api/terrapod/v1/policy-sets
```

Returns all policy sets. **Required permission:** `admin` or `audit`.

### Create Policy Set

```
POST /api/terrapod/v1/policy-sets
```

```json
{
  "data": {
    "type": "policy-sets",
    "attributes": {
      "name": "production-guardrails",
      "description": "Mandatory guardrails for production",
      "enforcement-level": "mandatory",
      "global-scope": false,
      "allow-labels": {"env": ["prod"]},
      "deny-names": ["prod-sandbox"]
    }
  }
}
```

`enforcement-level` is `advisory` (default) or `mandatory`. Scoping is `global-scope: true` (every workspace) or the `allow-labels` / `allow-names` / `deny-labels` / `deny-names` rules (same label model as roles; deny wins). **Required permission:** `admin`.

#### VCS-Sourced Policy Sets

Set `source` to `"vcs"` to create a policy set that syncs `.rego` files from a git repository instead of managing policies inline via the API:

```json
{
  "data": {
    "type": "policy-sets",
    "attributes": {
      "name": "security-baseline",
      "enforcement-level": "mandatory",
      "source": "vcs",
      "vcs-connection-id": "vcs-<uuid>",
      "vcs-repo-url": "https://github.com/org/policies",
      "vcs-branch": "main",
      "policy-path": "policies"
    }
  }
}
```

**VCS-specific attributes:**

| Attribute | Type | Description |
|---|---|---|
| `source` | string | `"inline"` (default) or `"vcs"` |
| `vcs-connection-id` | string | Required when `source=vcs`. References a VCS connection (`vcs-<uuid>` format). |
| `vcs-repo-url` | string | Required when `source=vcs`. HTTPS clone URL of the repo. |
| `vcs-branch` | string | Branch to track. Defaults to the repo's default branch if empty. |
| `policy-path` | string | Directory within the repo containing `.rego` files. Only direct children are loaded (no recursive descent). Empty string means repo root. |
| `vcs-last-commit-sha` | string | Read-only. SHA of the last successfully synced commit. |
| `vcs-last-synced-at` | string | Read-only. RFC 3339 timestamp of last successful sync. |
| `vcs-last-error` | string\|null | Read-only. Error message from the most recent sync attempt, or null. |

When `source=vcs`, inline policy CRUD is rejected with **409 Conflict** — policies are managed exclusively by the linked repository.

### Sync VCS Policy Set

```
POST /api/terrapod/v1/policy-sets/{id}/actions/sync
```

Triggers an immediate sync of a VCS-sourced policy set. Returns **202 Accepted** with the current policy set state; the actual sync runs asynchronously. Returns **409 Conflict** if the policy set has `source=inline`. **Required permission:** `admin`.

### Show / Update / Delete Policy Set

```
GET    /api/terrapod/v1/policy-sets/{id}     # policies embedded
PATCH  /api/terrapod/v1/policy-sets/{id}     # partial update
DELETE /api/terrapod/v1/policy-sets/{id}
```

Deleting a set removes its policies; recorded run evaluations are kept (set reference nulled, name snapshot retained). **Required permission:** `admin` (`admin`/`audit` for show).

### Manage Policies

```
POST   /api/terrapod/v1/policy-sets/{id}/policies   # add a policy
PATCH  /api/terrapod/v1/policies/{id}               # update
DELETE /api/terrapod/v1/policies/{id}
```

```json
{
  "data": {
    "type": "policies",
    "attributes": {
      "name": "no-public-buckets",
      "rego": "package terrapod\n\ndeny contains msg if { ... }"
    }
  }
}
```

The Rego is validated with `opa check` on create/update — broken Rego, or Rego that does not declare `package terrapod`, is rejected with 422. **Required permission:** `admin`.

### List Run Policy Evaluations

```
GET /api/terrapod/v1/runs/{run_id}/policy-evaluations
```

Returns the policy evaluations recorded for a run, plus a `meta.summary` (`status`: `passed` / `advisory-failed` / `blocked`, and counts). Each evaluation's `result` carries the per-policy violations/warnings. This is the endpoint behind the run's `policy-checks` relationship link. **Required permission:** `read` on the run's workspace.

### Override Run Policy

```
POST /api/terrapod/v1/runs/{run_id}/actions/override-policy
```

Overrides every failed/errored policy evaluation of a run and immediately re-drives a run held at the post-plan policy gate. **Required permission:** `admin` on the run's workspace.

### Runner protocol — Policy Bundle

```
GET /api/terrapod/v1/runs/{run_id}/policy-bundle
```

Returns the applicable policy sets + run/workspace context for a run. Used by the runner during the plan phase to drive OPA evaluation locally. A persistent fetch failure on the runner is **fatal** to the run (see [`docs/runners.md` — OPA Policy Evaluation](runners.md)) — there is no silent skip. Response shape is a flat JSON document (not JSON:API — the runner is the only consumer):

```json
{
  "policy_sets": [
    {
      "id": "polset-...",
      "name": "...",
      "enforcement_level": "mandatory",
      "policies": [
        {"id": "pol-...", "name": "...", "rego": "package terrapod\n..."}
      ]
    }
  ],
  "context": {
    "workspace": {"id": "...", "name": "...", "labels": {...}},
    "run": {"id": "...", "message": "...", "source": "...", "is_destroy": false, "plan_only": false}
  }
}
```

`policy_sets` is an empty list when nothing is in scope for the workspace — the runner then skips evaluation entirely. **Required permission:** runner token scoped to this `run_id`.

### Runner protocol — Policy Results

```
POST /api/terrapod/v1/runs/{run_id}/policy-results
```

```json
{
  "results": [
    {
      "policy_set_id": "polset-...",
      "policy_set_name": "...",
      "enforcement_level": "mandatory",
      "outcome": "failed",
      "result": {
        "policies": [
          {"policy": "...", "passed": false, "violations": ["..."], "warnings": [], "error": null}
        ],
        "evaluated_at": "2026-05-24T10:00:00Z"
      }
    }
  ]
}
```

Records the runner's policy-evaluation outcomes for the run. Persisted via Postgres `ON CONFLICT DO NOTHING` on `(run_id, policy_set_id)` so a retried POST after a transient failure is idempotent. The runner POSTs this **before** posting plan-result, so the API's post-plan gate sees the rows when it queries them. **Required permission:** runner token scoped to this `run_id`.

---

## Common Response Codes

| Code | Meaning |
|---|---|
| 200 | Success |
| 201 | Created |
| 204 | Deleted (no content) |
| 302 | Redirect (presigned URLs, binary cache, artifact downloads, OAuth flows) |
| 400 | Bad request (validation error) |
| 401 | Unauthorized (missing or invalid token) |
| 403 | Forbidden (insufficient permissions) |
| 404 | Not found |
| 409 | Conflict (lock conflict, duplicate resource, label change would reduce your access) |
| 422 | Unprocessable entity (semantic validation error) |
| 503 | Service unavailable (readiness check failed) |
