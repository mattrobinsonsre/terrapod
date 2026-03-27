# API Reference

Terrapod implements the TFE V2 API for compatibility with the `terraform` CLI, `go-tfe` client, and existing CI/CD integrations. All endpoints use JSON:API format.

The interactive API documentation is also available in the web UI under **API** in the navigation bar, offering both ReDoc and Swagger UI views.

![API Documentation (ReDoc)](images/api-docs-redoc.png)

---

## Base URL and Authentication

### Base URL

All API endpoints are prefixed with `/api/v2`. Example:

```
https://terrapod.example.com/api/v2/organizations/default/workspaces
```

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

Terrapod uses a single hardcoded organization: `default`. The `{org}` path parameter is accepted for CLI compatibility but only `default` is valid.

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
  "modules.v1": "/api/v2/registry/modules/",
  "providers.v1": "/api/v2/registry/providers/"
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
GET /api/v2/organizations/{org}
```

Returns organization details. Only `default` is valid.

### Entitlement Set

```
GET /api/v2/organizations/{org}/entitlement-set
```

Returns feature flags (all enabled for Terrapod).

---

## Workspaces

### List Workspaces

```
GET /api/v2/organizations/{org}/workspaces
```

### Get Workspace by Name

```
GET /api/v2/organizations/{org}/workspaces/{name}
```

### Get Workspace by ID

```
GET /api/v2/workspaces/{id}
```

### Create Workspace

```
POST /api/v2/organizations/{org}/workspaces
```

**Request body:**
```json
{
  "data": {
    "type": "workspaces",
    "attributes": {
      "name": "my-workspace",
      "auto-apply": false,
      "execution-mode": "remote",
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
DELETE /api/v2/workspaces/{id}
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

The following read-only attributes are included in workspace responses when drift detection is enabled:

| Attribute | Type | Description |
|---|---|---|
| `drift-last-checked-at` | string (RFC3339) or null | Timestamp of the last completed drift detection check |
| `drift-status` | string | Current drift status: `""` (never checked), `"no_drift"`, `"drifted"`, or `"errored"` |

### List VCS Refs

```
GET /api/v2/workspaces/{id}/vcs-refs
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
POST /api/v2/runs/{run_id}/actions/retry
```

Creates a new run from a terminal run (applied, errored, canceled, discarded) using the same workspace, configuration version, VCS metadata, and settings. Returns a 409 if the run is not in a terminal state.

**Required permission:** `plan` on the workspace (or `write` for apply runs).

### Workspace Events (SSE)

```
GET /api/v2/workspaces/{workspace_id}/runs/events
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

### Workspace List Events (SSE)

```
GET /api/v2/workspace-events
```

Server-Sent Events stream for the workspace list page. Emits events whenever any workspace changes (run status, lock, settings, state). The web UI uses this to refresh the workspace list without polling.

**Required permission:** Any authenticated user.

### Plan Details

```
GET /api/v2/runs/{run_id}/plan
```

Returns plan metadata and log download URL.

### Apply Details

```
GET /api/v2/runs/{run_id}/apply
```

Returns apply metadata and log download URL.

---

## Run Triggers

Run triggers create cross-workspace dependency chains. When a source workspace completes an apply, all downstream workspaces with an inbound trigger automatically get a new run queued.

### Create Run Trigger

```
POST /api/v2/workspaces/{id}/run-triggers
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
  https://terrapod.example.com/api/v2/workspaces/ws-abc123/run-triggers \
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
GET /api/v2/workspaces/{id}/run-triggers?filter[run-trigger][type]=inbound|outbound
```

- `inbound`: triggers where this workspace is the destination (what triggers runs here?)
- `outbound`: triggers where this workspace is the source (what does my apply trigger?)

The `filter[run-trigger][type]` parameter is required (422 if missing).

**Required permission:** `read` on the workspace.

**Example:**
```bash
curl -s \
  -H "Authorization: Bearer $TOKEN" \
  "https://terrapod.example.com/api/v2/workspaces/ws-abc123/run-triggers?filter[run-trigger][type]=inbound"
```

### Show Run Trigger

```
GET /api/v2/run-triggers/{id}
```

**Required permission:** `read` on the destination workspace.

### Delete Run Trigger

```
DELETE /api/v2/run-triggers/{id}
```

**Required permission:** `admin` on the destination workspace.

**Example:**
```bash
curl -s \
  -H "Authorization: Bearer $TOKEN" \
  -X DELETE \
  https://terrapod.example.com/api/v2/run-triggers/rt-abc123
```

---

## Configuration Versions

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
GET /api/v2/organizations/{org}/varsets
```

### Create Variable Set

```
POST /api/v2/organizations/{org}/varsets
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
GET  /api/v2/organizations/{org}/registry-modules
POST /api/v2/organizations/{org}/registry-modules
GET  /api/v2/organizations/{org}/registry-modules/private/default/{name}/{provider}
DELETE /api/v2/organizations/{org}/registry-modules/private/default/{name}/{provider}
POST /api/v2/organizations/{org}/registry-modules/private/default/{name}/{provider}/versions
DELETE /api/v2/organizations/{org}/registry-modules/private/default/{name}/{provider}/versions/{version}
```

### Update Module

```
PATCH /api/v2/organizations/{org}/registry-modules/private/default/{name}/{provider}
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

---

## Registry -- Providers

### CLI Protocol (for terraform init)

```
GET /api/v2/registry/providers/{namespace}/{type}/versions
GET /api/v2/registry/providers/{namespace}/{type}/{version}/download/{os}/{arch}
```

### TFE V2 Management API

```
GET  /api/v2/organizations/{org}/registry-providers
POST /api/v2/organizations/{org}/registry-providers
GET  /api/v2/organizations/{org}/registry-providers/private/default/{name}
DELETE /api/v2/organizations/{org}/registry-providers/private/default/{name}
POST /api/v2/organizations/{org}/registry-providers/private/default/{name}/versions
GET  /api/v2/organizations/{org}/registry-providers/private/default/{name}/versions
DELETE /api/v2/organizations/{org}/registry-providers/private/default/{name}/versions/{version}
POST /api/v2/organizations/{org}/registry-providers/private/default/{name}/versions/{version}/platforms
```

### Update Provider

```
PATCH /api/v2/organizations/{org}/registry-providers/private/default/{name}
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
GET    /api/registry/private/v2/gpg-keys
POST   /api/registry/private/v2/gpg-keys
GET    /api/registry/private/v2/gpg-keys/{namespace}/{key_id}
DELETE /api/registry/private/v2/gpg-keys/{namespace}/{key_id}
```

---

## Agent Pools

### List Pools

```
GET /api/v2/organizations/{org}/agent-pools
```

### Create Pool

```
POST /api/v2/organizations/{org}/agent-pools
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
GET /api/v2/agent-pools/{id}
```

### Delete Pool

```
DELETE /api/v2/agent-pools/{id}
```

### Pool Tokens

```
POST /api/v2/agent-pools/{id}/authentication-tokens
GET  /api/v2/agent-pools/{id}/authentication-tokens
```

### Listener Join

```
POST /api/v2/agent-pools/join
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
POST /api/v2/agent-pools/{pool_id}/listeners/join
```

### Listener Heartbeat

```
POST /api/v2/listeners/{id}/heartbeat
```

### Listener Certificate Renewal

```
POST /api/v2/listeners/{id}/renew
```

### Listener Run Polling

```
GET /api/v2/listeners/{id}/runs/next
```

Returns the next queued run for this listener.

### Listener Runner Token

```
POST /api/v2/listeners/{id}/runs/{run_id}/runner-token
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
PATCH /api/v2/listeners/{id}/runs/{run_id}
```

Reports run status changes (planning, planned, applying, applied, errored).

---

## VCS Connections

### List Connections

```
GET /api/v2/organizations/{org}/vcs-connections
```

### Create Connection

```
POST /api/v2/organizations/{org}/vcs-connections
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
GET /api/v2/vcs-connections/{id}
```

### Delete Connection

```
DELETE /api/v2/vcs-connections/{id}
```

---

## VCS Events (Webhooks)

### GitHub Webhook Receiver

```
POST /api/v2/vcs-events/github
```

Validates HMAC-SHA256 signature and triggers an immediate poll cycle. The webhook secret must match `TERRAPOD_VCS__GITHUB__WEBHOOK_SECRET`.

---

## Roles

### List Roles

```
GET /api/v2/roles
```

Returns built-in and custom roles.

**Required permission:** Platform `admin` or `audit`.

### Create Role

```
POST /api/v2/roles
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
GET /api/v2/roles/{name}
```

### Update Role

```
PATCH /api/v2/roles/{name}
```

### Delete Role

```
DELETE /api/v2/roles/{name}
```

Built-in roles cannot be deleted.

---

## Role Assignments

### List Assignments

```
GET /api/v2/role-assignments
```

**Required permission:** Platform `admin` or `audit`.

### Set Roles for User

```
PUT /api/v2/role-assignments
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
DELETE /api/v2/role-assignments/{provider}/{email}/{role}
```

---

## Authentication Tokens

### Create Token

```
POST /api/v2/users/{user_id}/authentication-tokens
```

### List Tokens

```
GET /api/v2/users/{user_id}/authentication-tokens
```

### Show Token

```
GET /api/v2/authentication-tokens/{id}
```

### Delete Token

```
DELETE /api/v2/authentication-tokens/{id}
```

---

## Run Artifacts (Runner)

Authenticated endpoints for runner Jobs to download inputs and upload outputs. All endpoints require a runner token (`Authorization: Bearer runtok:...`) scoped to the specified `run_id`.

### Download Config Archive

```
GET /api/v2/runs/{run_id}/artifacts/config
```

Returns 302 redirect to presigned storage URL for the configuration tarball.

### Download State

```
GET /api/v2/runs/{run_id}/artifacts/state
```

Returns 302 redirect to presigned storage URL for the current workspace state.

### Download Plan File

```
GET /api/v2/runs/{run_id}/artifacts/plan-file
```

Returns 302 redirect to presigned storage URL for the plan binary file.

### Upload Plan Log

```
PUT /api/v2/runs/{run_id}/artifacts/plan-log
Content-Type: application/octet-stream
```

Upload raw plan log bytes. Returns 204 on success.

### Upload Plan File

```
PUT /api/v2/runs/{run_id}/artifacts/plan-file
Content-Type: application/octet-stream
```

Upload plan binary file. Returns 204 on success.

### Upload Apply Log

```
PUT /api/v2/runs/{run_id}/artifacts/apply-log
Content-Type: application/octet-stream
```

Upload raw apply log bytes. Returns 204 on success.

### Upload State

```
PUT /api/v2/runs/{run_id}/artifacts/state
Content-Type: application/octet-stream
```

Upload new state after apply. Returns 204 on success.

---

## Binary Cache

### Download Binary

```
GET /api/v2/binary-cache/{tool}/{version}/{os}/{arch}
```

Returns a 302 redirect to a presigned URL for the binary. `tool` is `terraform` or `tofu`.

**Required:** Authentication (runner token, API token, or session).

### List Cached Binaries (Admin)

```
GET /api/v2/admin/binary-cache
```

### Warm Cache (Admin)

```
POST /api/v2/admin/binary-cache/warm
```

Pre-cache a specific tool version.

### Purge Cache (Admin)

```
DELETE /api/v2/admin/binary-cache/{tool}/{version}
```

---

## Health Dashboard

```
GET /api/v2/admin/health-dashboard
```

Returns platform-wide health data including workspace status summaries, recent run statistics, and listener availability. Intended for admin dashboards and monitoring integrations.

**Required permission:** Platform `admin` or `audit`.

**Response:**
```json
{
  "data": {
    "id": "health-dashboard",
    "type": "health-dashboards",
    "attributes": {
      "workspaces": {
        "total": 42,
        "locked": 2,
        "drift-enabled": 15,
        "by-drift-status": {
          "unchecked": 5,
          "no-drift": 30,
          "drifted": 5,
          "errored": 2
        },
        "stale": [{"id": "ws-uuid", "name": "prod-infra", "last-applied-at": "...", "days-since-apply": 18, "drift-status": "drifted"}]
      },
      "runs": {
        "queued": 4,
        "in-progress": 2,
        "recent-24h": {
          "total": 18,
          "applied": 12,
          "errored": 3,
          "canceled": 1
        },
        "average-plan-duration-seconds": 45,
        "average-apply-duration-seconds": 120
      },
      "listeners": {
        "total": 3,
        "online": 2,
        "offline": 1,
        "details": [{"id": "listener-uuid", "name": "pool-listener-pod", "pool-name": "default", "status": "online", "capacity": 5, "active-runs": 1, "last-heartbeat": "..."}]
      }
    }
  }
}
```

| Attribute | Description |
|---|---|
| `workspaces.total` | Total number of workspaces |
| `workspaces.locked` | Workspaces currently locked |
| `workspaces.drift-enabled` | Number of workspaces with drift detection enabled |
| `workspaces.by-drift-status` | Breakdown of workspaces by drift status |
| `workspaces.stale` | Top 20 workspaces by staleness (never-applied first) |
| `runs.queued` | Runs currently in `queued` state |
| `runs.in-progress` | Runs currently planning or applying |
| `runs.recent-24h` | 24-hour breakdown (total, applied, errored, canceled) |
| `runs.average-plan-duration-seconds` | Average plan duration (last 24h) |
| `runs.average-apply-duration-seconds` | Average apply duration (last 24h) |
| `listeners.total` | Total registered listeners |
| `listeners.online` | Listeners with a recent heartbeat (within 180s) |
| `listeners.offline` | Listeners with no recent heartbeat |
| `listeners.details` | Per-listener status, pool, capacity, and active runs |

### Health Dashboard Events (SSE)

```
GET /api/v2/admin/health-dashboard/events
```

Server-Sent Events stream for real-time admin health dashboard updates. The stream emits events whenever workspace or run health data changes.

**Required permission:** Platform `admin` or `audit`.

---

## Agent Pool Events (SSE)

```
GET /api/v2/agent-pools/{pool_id}/events
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
GET /api/v2/auth/providers
```

### Authorize (Start Login)

```
GET /api/v2/auth/authorize
```

### Callback (IDP Return)

```
GET /api/v2/auth/callback
POST /api/v2/auth/callback
```

### Active Sessions

```
GET /api/v2/auth/sessions
```

### Logout

```
POST /api/v2/auth/logout
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
GET /api/v2/admin/audit-log
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
curl "https://terrapod.example.com/api/v2/admin/audit-log?filter[actor]=admin@example.com&page[size]=10" \
  -H "Authorization: Bearer $TERRAPOD_TOKEN"
```

---

## Users

User management endpoints. List and show require `admin` or `audit` role. Update and delete require `admin` role.

### List Users

```
GET /api/v2/organizations/{org}/users
```

**Query Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `filter[email]` | string | Filter by email (case-insensitive substring match) |
| `page[number]` | integer | Page number (default: 1) |
| `page[size]` | integer | Page size (default: 20, max: 100) |

### Show User

```
GET /api/v2/users/{email}
```

### Update User

```
PATCH /api/v2/users/{email}
```

**Updatable attributes:** `is-active`, `display-name`.

When `is-active` is set to `false`, all sessions for that user are revoked immediately.

### Delete User

```
DELETE /api/v2/users/{email}
```

Cascades: revokes all sessions, deletes all role assignments.

---

## Notification Configurations

Workspace-scoped notifications that fire on run lifecycle events. Three destination types: **generic** (webhook with HMAC-SHA512 signing), **slack** (Block Kit formatted), and **email** (SMTP).

### Create Notification Configuration

```
POST /api/v2/workspaces/{id}/notification-configurations
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
GET /api/v2/workspaces/{id}/notification-configurations
```

**Required permission:** `read` on the workspace.

### Show Notification Configuration

```
GET /api/v2/notification-configurations/{id}
```

**Required permission:** `read` on the associated workspace.

### Update Notification Configuration

```
PATCH /api/v2/notification-configurations/{id}
```

Same body format as create. Only include attributes to change. Token is never returned in responses — only `has-token: true/false`.

**Required permission:** `admin` on the workspace.

### Delete Notification Configuration

```
DELETE /api/v2/notification-configurations/{id}
```

**Required permission:** `admin` on the workspace.

### Verify Notification Configuration

```
POST /api/v2/notification-configurations/{id}/actions/verify
```

Sends a test payload to the configured destination and returns the delivery response.

**Required permission:** `admin` on the workspace.

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
