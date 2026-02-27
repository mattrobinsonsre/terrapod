# Architecture

This document describes the internal architecture of Terrapod, covering system components, data flow, storage abstractions, the runner execution layer, authentication flows, and VCS integration.

---

## System Components

```
+-------------------------------------------------------------------+
|                        Kubernetes Cluster                          |
|                                                                    |
|  +---------------------+    +----------------------------------+  |
|  |   Ingress            |    |   terrapod namespace              |  |
|  |   (nginx / traefik)  |--->|                                  |  |
|  +---------------------+    |  +-------------+  +-----------+  |  |
|                              |  | Next.js Web |  | FastAPI   |  |  |
|                              |  | (BFF proxy) |->| API       |  |  |
|                              |  +-------------+  +-----+-----+  |  |
|                              |                         |         |  |
|                              |  +-------------+  +-----+-----+  |  |
|                              |  | Runner      |  | Migrations |  |  |
|                              |  | Listener    |  | (Alembic)  |  |  |
|                              |  +------+------+  +-----------+  |  |
|                              +---------|-------------------------+  |
|                                        |                           |
|  +-------------------------------------|-------------------------+ |
|  |   runner namespace                  |                         | |
|  |                           +---------v---------+               | |
|  |                           | K8s Job (runner)  |               | |
|  |                           | terraform / tofu  |               | |
|  |                           +-------------------+               | |
|  +---------------------------------------------------------------+ |
+-------------------------------------------------------------------+
         |              |              |
   +-----v----+  +-----v----+  +-----v---------+
   | PostgreSQL|  |  Redis   |  | Object Storage|
   | (external)|  | (external|  | (S3/Azure/GCS |
   +----------+   +----------+  |  /filesystem) |
                                +---------------+
```

### Component Responsibilities

| Component | Purpose | Implementation |
|---|---|---|
| **Next.js Web** | Single ingress entry point; serves UI pages and proxies API calls | Next.js 15, React 19, Tailwind CSS, Radix UI |
| **FastAPI API** | All business logic, TFE V2 API, auth, registry, VCS polling | Python 3.13+, FastAPI, SQLAlchemy async, Pydantic |
| **Runner Listener** | Polls for queued runs, creates K8s Jobs, streams logs | Same Python codebase as API, different entrypoint |
| **Runner Jobs** | Ephemeral containers that execute `terraform` or `tofu` | Minimal Alpine image with curl/tar/jq |
| **PostgreSQL** | Relational data: users, workspaces, state metadata, runs, registry | PostgreSQL 14+ |
| **Redis** | Sessions, auth state, listener heartbeats, API token role cache | Redis 7+ |
| **Object Storage** | State files, config tarballs, plan outputs, logs, registry artifacts, cache | S3, Azure Blob, GCS, or filesystem |

---

## BFF (Backend For Frontend) Pattern

All traffic enters through the Next.js frontend via a single Ingress rule. The browser never communicates directly with the FastAPI API server.

```
Browser                Next.js (port 3000)         FastAPI API (port 8000)
  |                         |                              |
  |--- GET /workspaces ---->|                              |
  |    (page render)        |                              |
  |<--- HTML + JS ----------|                              |
  |                         |                              |
  |--- GET /api/v2/... ---->|                              |
  |    (data fetch)         |--- proxy /api/* ------------>|
  |                         |<-- JSON response ------------|
  |<--- JSON response ------|                              |
```

**How it works:**

- `next.config.js` defines rewrites: `/api/*` and `/.well-known/*` are proxied to the API service via the `API_URL` environment variable (e.g., `http://terrapod-api:8000`)
- The Ingress has a single backend: the web service
- This eliminates CORS entirely -- all requests are same-origin from the browser's perspective
- In production, only the web service needs to be exposed; the API service is cluster-internal

**Source files:**
- `web/next.config.js` -- rewrite rules
- `helm/terrapod/templates/ingress.yaml` -- single-backend Ingress
- `helm/terrapod/templates/deployment-web.yaml` -- API_URL env var injection

---

## Storage Abstraction

Terrapod uses a protocol-based storage abstraction that supports four backends through their native SDKs. There is no S3 compatibility shim or MinIO dependency.

### ObjectStore Protocol

Defined in `services/terrapod/storage/protocol.py`:

```
ObjectStore Protocol
  |
  +-- put(key, data, content_type) -> None
  +-- get(key) -> bytes
  +-- delete(key) -> None
  +-- head(key) -> ObjectMeta
  +-- list(prefix) -> list[str]
  +-- presigned_get(key, expires) -> PresignedURL
  +-- presigned_put(key, content_type, expires) -> PresignedURL
```

### Backend Implementations

| Backend | Module | Auth | Use Case |
|---|---|---|---|
| **AWS S3** | `storage/s3.py` | IAM / IRSA | Production (AWS) |
| **Azure Blob** | `storage/azure.py` | Managed Identity / connection string | Production (Azure) |
| **GCS** | `storage/gcs.py` | Workload Identity / service account | Production (GCP) |
| **Filesystem** | `storage/filesystem.py` | HMAC-signed URLs | Local dev, CI |

### Storage Key Layout

All storage paths are constructed via `storage/keys.py`:

```
state/{workspace_id}/{version_id}.tfstate       # State files (Fernet-encrypted)
config/{workspace_id}/{config_version_id}.tar.gz # Configuration tarballs
plans/{run_id}/plan.json                         # Plan output
logs/{run_id}/plan.log                           # Plan logs
logs/{run_id}/apply.log                          # Apply logs
registry/modules/{org}/{ns}/{name}/{prov}/{ver}.tar.gz   # Private modules
registry/providers/{org}/{ns}/{name}/{ver}/...            # Private providers
cache/modules/{host}/{ns}/{name}/{prov}/{ver}.tar.gz     # Cached modules
cache/providers/{host}/{ns}/{type}/{ver}/{file}          # Cached providers
cache/binaries/{tool}/{ver}/{os}/{arch}/{file}           # Cached CLI binaries
```

### Presigned URLs

All file uploads and downloads use presigned URLs. The API generates time-limited URLs; clients upload/download directly to/from storage. This keeps large files off the API server.

For the filesystem backend, URLs are HMAC-signed and served by `storage/filesystem_routes.py` endpoints on the API server itself.

---

## Runner Architecture (ARC Pattern)

Terrapod's execution layer follows the Actions Runner Controller (ARC) pattern: a long-lived controller (the runner listener) watches for queued runs and creates ephemeral Kubernetes Jobs.

### Execution Flow

```
1. User/VCS creates a Run (status: pending)
        |
2. Run transitions to "queued"
        |
3. Listener polls: GET /api/v2/listeners/{id}/runs/next
        |
4. Listener creates K8s Job in runner namespace
   - Image: terrapod-runner (minimal Alpine)
   - Resources: from workspace config (cpu/memory requests + 2x limits)
   - Env vars: workspace variables + Terraform vars
   - Service account: from agent pool config (for cloud identity)
        |
5. Runner Job starts:
   a. Fetches terraform/tofu binary from binary cache
   b. Downloads configuration tarball
   c. Runs terraform init (providers via network mirror)
   d. Runs terraform plan (streams logs to object storage)
   e. Reports plan status to API
        |
6. If auto_apply or user confirms:
   a. Runs terraform apply
   b. Streams apply logs to object storage
   c. Reports apply status to API
        |
7. Job completes, TTL controller cleans up after 10 minutes
```

### Signal Forwarding and Graceful Termination

Runner Jobs handle SIGTERM gracefully for spot instance preemption:

```
K8s sends SIGTERM
    |
    v
runner-entrypoint.sh (traps SIGTERM/SIGQUIT)
    |
    v
Forwards signal to terraform/tofu child process
    |
    v
Terraform finishes current API call
    |
    v
Releases state lock
    |
    v
Exits cleanly
    |
    (120s terminationGracePeriodSeconds; SIGKILL if exceeded)
```

The entrypoint script is at `docker/runner-entrypoint.sh`.

### Local vs Remote Listeners

**Local listener** (in-cluster):
- Deployed as a Kubernetes Deployment in the same cluster
- Uses the same Docker image as the API with a different entrypoint (`python -m terrapod.runner.listener`)
- Has RBAC permissions to create/watch/delete Jobs and Pods in the runner namespace
- Authenticates to the API via internal service URL

**Remote listener** (agent pool):
- Deployed in a separate cluster
- Joins via join token, receives X.509 certificate (Ed25519)
- Authenticates via `X-Terrapod-Client-Cert` header
- Heartbeats every 60s (180s TTL in Redis)

### Per-Workspace Resources

Each workspace has `resource_cpu` and `resource_memory` columns:

| Setting | Default | Description |
|---|---|---|
| `resource_cpu` | `1` | CPU request for runner Jobs |
| `resource_memory` | `2Gi` | Memory request for runner Jobs |

Limits are computed as 2x the requests automatically. Values are snapshotted to the `runs` table at run creation time so they remain stable even if the workspace is later modified.

---

## Certificate Authority

Terrapod includes a built-in Certificate Authority for authenticating remote runner listeners.

```
CA Initialization (first startup)
    |
    v
Generate Ed25519 keypair
CN = "Terrapod Certificate Authority"
Store in certificate_authority DB table (single row)
    |
    v
Remote Listener Join Flow:
    1. Admin creates agent pool + join token
    2. Listener calls POST /api/v2/agent-pools/{pool_id}/listeners/join
    3. API validates join token (SHA-256 hash, expiry, max_uses)
    4. API issues X.509 certificate with SAN URIs:
       - terrapod://listener/{name}
       - terrapod://pool/{pool_name}
    5. Returns: listener ID, certificate, CA cert
    |
    v
Ongoing Authentication:
    - Listener sends X-Terrapod-Client-Cert header (base64 PEM)
    - API verifies: CA signature, expiry, CN->DB lookup, fingerprint match
    |
    v
Certificate Renewal:
    - At 50% of validity: POST /api/v2/listeners/{id}/renew
    - No re-registration needed on restart if stored cert is valid
```

**Source files:**
- `services/terrapod/auth/ca.py` -- CA keypair generation, certificate issuance
- `services/terrapod/api/routers/agent_pools.py` -- join and renew endpoints
- `services/terrapod/runner/identity.py` -- local vs remote identity establishment

---

## Authentication Flows

### Web UI Login (Session-Based)

```
Browser                  Next.js              API               IDP (OIDC/SAML)
  |                         |                   |                     |
  |-- GET /login ---------->|                   |                     |
  |<-- Login page ----------|                   |                     |
  |                         |                   |                     |
  |-- Click SSO button ---->|                   |                     |
  |                         |-- GET /api/v2/auth/authorize ---------->|
  |                         |<-- redirect URL --|                     |
  |<-- 302 redirect --------|                   |                     |
  |                         |                   |                     |
  |-- Follow redirect ------------------------------------------------>|
  |<-- IDP login page ------------------------------------------------|
  |-- Authenticate -------------------------------------------------->|
  |<-- 302 to /auth/callback?code=xxx&state=yyy ----------------------|
  |                         |                   |                     |
  |-- GET /auth/callback?...                    |                     |
  |                         |-- validate state -->                    |
  |                         |-- exchange code --->                    |
  |                         |<-- session token --|                    |
  |<-- Set session, redirect to / --------------|                    |
```

### Terraform CLI Login (OAuth2 PKCE)

```
terraform login terrapod.local
  |
  |-- GET /.well-known/terraform.json
  |   Returns: { "login.v1": { "client": "terraform-cli", "grant_types": ["authz_code"],
  |              "authz": "/oauth/authorize", "token": "/oauth/token", ... } }
  |
  |-- Opens browser to /oauth/authorize?
  |   response_type=code&client_id=terraform-cli&
  |   code_challenge=xxx&code_challenge_method=S256&
  |   redirect_uri=urn:ietf:wg:oauth:2.0:oob:auto&state=yyy
  |
  |-- API stores auth state in Redis (5min TTL), redirects to IDP
  |-- User authenticates with IDP
  |-- IDP callback generates one-time auth code (60s TTL in Redis)
  |-- Browser receives auth code, terraform CLI extracts it
  |
  |-- POST /oauth/token
  |   grant_type=authorization_code&code=xxx&code_verifier=yyy
  |
  |-- API validates PKCE, creates API token in PostgreSQL
  |-- Returns: { "access_token": "{id}.tpod.{secret}", "token_type": "bearer" }
  |
  |-- terraform stores token in ~/.terraform.d/credentials.tfrc.json
```

### Unified Auth Dependency

The API uses a single auth dependency (`api/dependencies.py:get_current_user`) for all endpoints:

```
Authorization: Bearer <token>
  |
  v
1. Try API token lookup:
   - SHA-256 hash the token
   - Query api_tokens table by hash
   - Check max TTL (created_at + config TTL)
   - Resolve roles from role_assignments + platform_role_assignments
  |
  v (not found)
2. Try session lookup:
   - Query Redis: tp:session:{token}
   - Slide TTL on hit (12h)
   - Return cached user + roles
  |
  v (not found)
3. Return 401 Unauthorized
```

---

## VCS Integration

Terrapod uses a polling-first design for VCS integration. No inbound connections are required -- only outbound HTTPS to VCS provider APIs.

```
+-------------------+                    +------------------+
|  API Server       |                    |  VCS Providers   |
|                   |                    |                  |
|  +-------------+  |   HTTPS (outbound) |  +------------+ |
|  | VCS Poller  |--+-------------------->  | GitHub API | |
|  | (async task)|  |    every 60s       |  +------------+ |
|  +------+------+  |                    |  +------------+ |
|         |         |   HTTPS (outbound) |  | GitLab API | |
|         |         +-------------------->  +------------+ |
|         |         |                    +------------------+
|         v         |
|  For each workspace with VCS:          +------------------+
|  1. Check branch HEAD SHA              | Optional:        |
|  2. Check open PRs/MRs                 | GitHub webhook   |
|  3. If new SHA detected:               | POST /api/v2/    |
|     - Download tarball                 | vcs-events/github|
|     - Create ConfigurationVersion      +--------+---------+
|     - Queue Run                                 |
|                                    triggers immediate poll
```

### Provider Dispatch

The `VCSProvider` protocol (`services/terrapod/services/vcs_provider.py`) defines the interface. The poller dispatches to the correct provider based on the VCS connection's `provider` field:

| Operation | GitHub | GitLab |
|---|---|---|
| Get branch SHA | GitHub API (installation token) | GitLab API (access token) |
| Get default branch | GitHub API | GitLab API |
| Download archive | GitHub API (tarball) | GitLab API (tarball) |
| List open PRs/MRs | GitHub API (pulls) | GitLab API (merge requests) |
| Parse repo URL | github.com/org/repo | gitlab.com/group/project |

For detailed setup instructions, see [vcs-integration.md](vcs-integration.md).

---

## Run State Machine

```
pending -----> queued -----> planning -----> planned -----> confirmed -----> applying -----> applied
                                |               |                               |
                                v               v                               v
                             errored         discarded                       errored

Any non-terminal state -----> canceled (user action)
```

**Terminal states:** `applied`, `errored`, `discarded`, `canceled`

**Key behaviors:**
- `auto_apply=true`: planned transitions automatically to confirmed, then applying
- `auto_apply=false`: planned waits for user confirmation
- Workspace is locked during an active run and unlocked on terminal state
- Queue dispatch uses `SELECT ... FOR UPDATE SKIP LOCKED` (PostgreSQL job queue pattern)
- Plan-only (speculative) runs skip the apply phase entirely

---

## Database Schema

The database schema is managed by Alembic migrations in `alembic/versions/`. Key models (defined in `services/terrapod/db/models.py`):

| Model | Purpose |
|---|---|
| `User` | User accounts (email, provider, hashed password) |
| `Role` | Custom roles with allow/deny labels and workspace_permission |
| `RoleAssignment` | Maps (provider, email) to custom roles |
| `PlatformRoleAssignment` | Maps (provider, email) to platform roles (admin, audit) |
| `APIToken` | Long-lived API tokens (SHA-256 hashed) |
| `Workspace` | Workspace config, VCS settings, labels, owner |
| `StateVersion` | State version metadata (serial, lineage, MD5) |
| `Variable` | Per-workspace variables (sensitive values Fernet-encrypted) |
| `VariableSet` | Org-scoped variable sets with workspace assignments |
| `ConfigurationVersion` | Uploaded configuration tarballs |
| `Run` | Run lifecycle (status, timestamps, VCS metadata, resources) |
| `AgentPool` | Named runner pool with service account |
| `AgentPoolToken` | Join tokens for remote listeners |
| `RunnerListener` | Registered listener identity and certificate |
| `CertificateAuthorityModel` | CA keypair for listener certificates |
| `VCSConnection` | VCS provider auth config (GitHub App or GitLab token) |
| `RegistryModule` / `RegistryModuleVersion` | Private module registry |
| `RegistryProvider` / `RegistryProviderVersion` / `RegistryProviderPlatform` | Private provider registry |
| `GPGKey` | GPG keys for provider signing |
| `CachedModule` | Pull-through module cache entries |
| `CachedProviderPackage` | Pull-through provider cache entries |
| `CachedBinary` | Pull-through CLI binary cache entries |

---

## Configuration

Terrapod uses a layered configuration system:

1. **YAML config** -- mounted at `/etc/terrapod/config.yaml` (from Helm ConfigMap)
2. **Environment variables** -- prefix `TERRAPOD_`, nested with `__` delimiter
3. Environment variables override YAML values

Example: `TERRAPOD_STORAGE__BACKEND=s3` overrides `storage.backend` from YAML.

Runner configuration is separate, loaded from `/etc/terrapod/runners.yaml`.

**Source:** `services/terrapod/config.py`
