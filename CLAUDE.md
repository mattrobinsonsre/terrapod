# Terrapod

## Project Vision

Terrapod is a free, open-source **platform** replacement for Terraform Enterprise (TFE). It is **not** a fork of Terraform or OpenTofu — it provides the collaboration, governance, state management, and UI layer that wraps around `terraform` or `tofu` as pluggable execution backends.

Terrapod targets **API compatibility with the [HCP Terraform / TFE V2 API](https://developer.hashicorp.com/terraform/enterprise/api-docs)** so that existing tooling (the `terraform` CLI with `cloud` block, the [`go-tfe`](https://pkg.go.dev/github.com/hashicorp/go-tfe) client, CI/CD integrations) can point at a Terrapod instance with minimal reconfiguration.

## Scope — Feature Parity Targets

The features below are drawn from Terraform Enterprise. Each is categorised by priority.

### P0 — Core (MVP)

| Feature | Description |
|---|---|
| **Workspaces** | ✅ Single "default" organization; workspaces isolate state, variables, and runs (implemented: CRUD, lock/unlock, state versioning) |
| **Remote State Management** | ✅ Versioned state storage with locking; state rollback (implemented: local execution mode with remote state via `cloud` block) |
| **Remote Execution Mode** | ✅ Plan/apply runs on the server via runner infrastructure (implemented: K8s Job-based execution, ARC pattern, local + remote listeners) |
| **VCS Integration** | ✅ Multi-provider VCS integration with polling-first design; GitHub (App) and GitLab (access token) support; automatic runs on push + speculative plans on PRs/MRs (implemented: VCS connections, provider dispatch, background poller, optional webhook accelerator) |
| **Variables & Sensitive Variables** | ✅ Per-workspace env and Terraform variables; Fernet-encrypted at rest; variable sets with workspace assignment and precedence (implemented) |
| **Team & RBAC** | ✅ Label-based RBAC with hierarchical workspace permissions (read/plan/write/admin); workspace ownership; registry RBAC; role + assignment CRUD; API token role resolution (implemented: no teams — labels replace teams entirely) |
| **API (V2-compatible)** | ✅ JSON:API surface compatible with `go-tfe` / `terraform login` (implemented: ping, account, org, entitlements, workspaces, state versions, lock/unlock) |
| **CLI-Driven Runs** | ✅ `terraform plan` / `apply` via cloud backend with local execution (both `terraform` and `tofu` CLI verified) |
| **Run Triggers** | Cross-workspace dependency triggers |

### P1 — Governance & Security

| Feature | Description |
|---|---|
| **Policy as Code (OPA)** | OPA (Rego) policy evaluation on plan output (Sentinel is proprietary — OPA is the open alternative) |
| **Cost Estimation** | Pre-apply cost estimates for AWS / GCP / Azure via [Infracost](https://www.infracost.io/) integration |
| **Audit Logging** | Immutable event log for compliance |
| **SSO / SAML / OIDC** | Enterprise identity provider integration |
| **Dynamic Provider Credentials** | Short-lived cloud credentials via OIDC workload identity |
| **Run Tasks** | Pre/post-plan webhook hooks for external validation |

### P2 — Operational Excellence

| Feature | Description |
|---|---|
| **Private Module Registry** | ✅ Publish, version, and share modules and providers internally (implemented: module + provider registry, GPG keys, module caching, provider caching, binary caching) |
| **Agent Pools** | ✅ Named groups of remote runner listeners; join token → certificate exchange for cross-cluster execution (implemented: CRUD, join flow, heartbeat, cert renewal) |
| **No-Code Provisioning** | Self-service UI for deploying registry modules without writing HCL |
| **Ephemeral Workspaces** | Auto-destroy after configurable TTL |
| **Drift Detection** | Scheduled plan-only runs to detect out-of-band changes |
| **Notifications** | Slack, webhook, and email notifications on run events |
| **Health Dashboard** | Workspace health and staleness metrics |

### Explicitly Out of Scope

- **Terraform/OpenTofu engine** — Terrapod orchestrates `terraform` and `tofu`; it does not reimplement them
- Sentinel policy language (proprietary to HashiCorp)
- Terraform Cloud Business tier features (e.g. self-service agents marketplace)
- Built-in Vault integration (users can configure Vault externally)
- Non-Kubernetes deployment (no Docker Compose, no bare-metal installers)

## Architecture Principles

1. **API-first** — every UI action is backed by a public API endpoint; the V2 API is the contract
2. **OpenTofu-friendly** — support both `terraform` and `tofu` as execution backends; Terrapod is the platform, not the engine
3. **Postgres + Native Object Storage** — Postgres for relational data; native cloud object storage (AWS S3, Azure Blob, GCP GCS) with filesystem fallback for dev. No S3 compatibility shim — the backend speaks each provider's native SDK. No MinIO (project is dead/archived)
4. **Kubernetes-native** — deployed exclusively via Helm chart on Kubernetes; no other deployment targets
5. **ARC-pattern execution** — a runner listener (long-lived Deployment) watches for queued runs and spawns ephemeral K8s Jobs with resource limits from named runner definitions. Follows the GitHub Actions Runner Controller model: controller creates Jobs, not persistent runners
6. **Bring your own auth** — support local accounts, OIDC, SAML; no baked-in IdP dependency
7. **Modern UX** — the web UI is a polished, modern React application; design quality is a first-class concern
8. **BFF (Backend For Frontend)** — the Next.js frontend is the single ingress entry point; it proxies `/api/*` and `/.well-known/*` to the API service via Next.js rewrites. The browser never talks to the API directly. This simplifies ingress (one rule, one backend) and keeps CORS out of the picture
9. **Single organization** — all users and resources belong to a hardcoded "default" organization. Multi-org is a Terraform Cloud concept for SaaS multi-tenancy; Terrapod is self-hosted, so one org per instance is sufficient. The TFE V2 API still accepts `{org}` in paths for CLI compatibility but only "default" is valid
10. **RFC3339 timestamps** — all datetimes in the database are timezone-aware UTC (`TIMESTAMPTZ`). The API always serializes timestamps as RFC3339 with trailing `Z` (e.g. `2025-01-01T00:00:00Z`), never `+00:00`. This is required for `go-tfe` client compatibility

## Authentication Architecture

Two credential types share a single `Authorization: Bearer <token>` header:

- **Sessions** (Redis) — short-lived (12h sliding TTL), for web UI. Created via local password login or SSO (OIDC/SAML) callback. Stored in Redis with `tp:session:` prefix.
- **API tokens** (PostgreSQL) — long-lived, for terraform CLI and automation. SHA-256 hashed at rest. Only the raw token value is returned once at creation time. Token format: `{random_id}.tpod.{random_secret}`. Max lifetime enforced via `auth.api_token_max_ttl_hours` config (computed at validation time as `created_at + max_ttl`; 0 = no limit). Changing the config retroactively affects all existing tokens.

The unified auth dependency (`api/dependencies.py:get_current_user`) tries API token lookup first (SHA-256 hash + indexed DB query), then Redis session lookup. Both return the same `AuthenticatedUser` shape.

### Terraform Login Flow

`terraform login` uses OAuth2 Authorization Code + PKCE:

1. `GET /.well-known/terraform.json` — service discovery
2. `GET /oauth/authorize` — stores auth state with `credential_type="api_token"`, redirects to SSO provider
3. IDP callback (`/auth/callback`) — shared with web UI, generates one-time auth code
4. `POST /oauth/token` — validates PKCE, creates API token in PostgreSQL, returns `{"access_token": "...", "token_type": "bearer"}`

### SSO Connector System

Ported from bamf. Abstract base class `SSOConnector` with three implementations:

- **LocalConnector** — password auth against the `users` table (PBKDF2-SHA256)
- **OIDCConnector** — authlib-based OIDC (Auth0, Okta, Azure AD, etc.)
- **SAMLConnector** — python3-saml (Azure AD SAML, etc.)

Connectors are registered at startup by `init_connectors()`. Client secrets are injected from `TERRAPOD_{NAME}_CLIENT_SECRET` environment variables.

### Role Resolution

Login triggers role resolution from three sources (merged, deduplicated):

1. IDP groups from connector (with role prefix stripping)
2. Claims-to-roles config mapping rules
3. Internal `role_assignments` table (per provider+email)

Built-in roles: `admin` (bypasses RBAC), `audit` (read-only), `everyone` (implicit for all users).

API tokens resolve the user's roles from `role_assignments` + `platform_role_assignments` at request time, cached in Redis (`tp:token_roles:{email}`, 60s TTL).

### Workspace Permission Model

No teams — label-based RBAC replaces TFE's team model entirely. A "team" is just a label.

#### Permission Levels (hierarchical)

| Level | Grants |
|---|---|
| **read** | View workspace, view runs & plan output, view state metadata, view non-sensitive variables |
| **plan** | read + queue plan-only runs, lock/unlock (own locks), download raw state |
| **write** | plan + confirm/discard applies, create apply runs, CRUD variables, upload state/config |
| **admin** | write + update/delete workspace, change VCS/execution settings, change labels. Cannot change owner (platform admin only) |

#### Permission Resolution Order (highest wins)

1. **Platform `admin`** → `admin` on all workspaces
2. **Platform `audit`** → `read` on all workspaces
3. **Workspace owner** (`ws.owner_email == user.email`) → `admin`
4. **Label-based RBAC** — for each custom role the user holds: if workspace matches allow rules AND doesn't match deny rules, collect that role's `workspace_permission`. Take the highest.
5. **`everyone` role** — if workspace has label `access: everyone`, grant `read`
6. **Default** → no access

Custom roles carry a `workspace_permission` field (read/plan/write/admin) — single value, not a list, since the four levels are strictly hierarchical.

#### Registry Permissions

Modules and providers follow the same owner + label RBAC model as workspaces but with three levels (read/write/admin — no "plan" concept). Resolution order is identical. A role's `workspace_permission` maps to registry permission: `plan` → `read`.

#### Platform Permissions

| Operation | Required |
|---|---|
| Manage roles & assignments | `admin` |
| Manage VCS connections | `admin` |
| Manage agent pools & tokens | `admin` |
| Binary/module/provider cache admin | `admin` |
| View roles, VCS connections, agent pools | `admin` or `audit` |
| Create workspaces | Any authenticated user (creator becomes owner) |
| Create registry modules/providers | Any authenticated user (creator becomes owner) |
| Variable sets (create/update/delete) | `admin` |

### Key Redis Prefixes

| Prefix | Purpose | TTL |
|---|---|---|
| `tp:session:{token}` | Session data | 12h sliding |
| `tp:user_sessions:{email}` | Session set per user | 12h |
| `tp:auth_state:{idp_state}` | Auth state (authorize → callback) | 5 min |
| `tp:auth_code:{code}` | Auth code (callback → token) | 60 sec |
| `tp:recent_user:{provider}:{email}` | Recent user tracking | 7 days |
| `tp:token_roles:{email}` | API token role cache | 60s |
| `tp:listener:{id}:status` | Listener online/offline | 180s |
| `tp:listener:{id}:heartbeat` | Last heartbeat timestamp | 180s |
| `tp:listener:{id}:capacity` | Max concurrent runs | 180s |
| `tp:listener:{id}:active_runs` | Count of running Jobs | — |
| `tp:listener:{id}:runner_defs` | Supported runner def names (JSON) | 180s |

## Runner Architecture

Terrapod's execution layer follows the **ARC (Actions Runner Controller) pattern**: a controller (the runner listener) creates ephemeral K8s Jobs rather than maintaining persistent runner processes.

### Per-Workspace Resources

Each workspace has `resource_cpu` and `resource_memory` columns that define the K8s resource **requests** for runner Jobs. Defaults: 1 CPU / 2Gi memory. The listener computes **limits as 2x the requests** automatically (e.g. 1 CPU request → 2 CPU limit). These values are snapshotted to the `runs` table at run creation time so they remain stable even if the workspace is later modified.

### Graceful Termination

Runner Jobs use `terminationGracePeriodSeconds: 120` in the pod spec. The runner container has a signal-forwarding entrypoint (`docker/runner-entrypoint.sh`) that traps SIGTERM/SIGQUIT and forwards them to the terraform/tofu child process. This is critical for spot instance preemption: terraform receives SIGTERM → finishes current API call → releases state lock → exits cleanly. If terraform doesn't exit within 120s, K8s sends SIGKILL.

### Local Execution Flow

```
Workspace → Queue run → Listener polls API → Create K8s Job (per-workspace resources)
                                                                        ↓
                                              API ← Report status ← Job runs terraform/tofu
                                                                        ↓
                                              Object storage ← Stream logs via presigned URLs
                                                                        ↓
                                              TTL controller cleans up finished Job
```

The local listener is a Deployment in the same cluster, using the same Docker image as the API (`python -m terrapod.runner.listener`). It has RBAC to create/watch/delete Jobs and Pods in the runner namespace.

### Remote Execution (Agent Pools)

Remote listeners run in separate clusters. They join the central API using a join token → certificate exchange:

1. Admin creates an **agent pool** (named group, e.g. "aws-prod", "on-prem-dc1")
2. Admin generates a **join token** for the pool (SHA-256 hashed in DB, with expiry + max_uses)
3. Remote listener deployed with join token → calls `POST /api/v2/agent-pools/{pool_id}/listeners/join`
4. Receives: listener ID, X.509 certificate (Ed25519), CA cert
5. Ongoing: heartbeat every 60s (Redis-backed, 180s TTL), polls run queue, streams logs

### Database Models

- **AgentPool** — named group; default pool = local in-cluster listener
- **AgentPoolToken** — join token (SHA-256 hashed, expiry, max_uses, revocable)
- **RunnerListener** — durable identity (name, certificate fingerprint, supported definitions); runtime state in Redis

### Certificate Authority

- Ed25519 CA keypair, CN="Terrapod Certificate Authority"
- Persisted in `certificate_authority` DB table (single row, created on first startup via `init_ca()`)
- Listener certificates issued with SAN URIs: `terrapod://listener/{name}`, `terrapod://pool/{pool_name}`
- Certificate auth via `X-Terrapod-Client-Cert` header (base64-encoded PEM)
- `get_listener_identity` dependency verifies CA signature, expiry, CN→DB lookup, fingerprint match

### Certificate Lifecycle

- Issued on join, stored in K8s Secret (or filesystem for bare-metal)
- Renewal at 50% of validity via `POST /api/v2/listeners/:id/renew`
- No re-registration needed on restart if stored certificate is still valid

### Run State Machine

```
pending → queued → planning → planned → [confirmed] → applying → applied
                      ↓          ↓                        ↓
                   errored    discarded                 errored

Any non-terminal state → canceled (user action)
```

- `auto_apply=true`: planned → confirmed → applying (automatic)
- `auto_apply=false`: planned → user confirms → confirmed → applying
- Workspace locked during active run, unlocked on terminal state
- Configuration versions: create → upload tarball → auto-queue pending runs
- Queue dispatch: `SELECT ... FOR UPDATE SKIP LOCKED` (Postgres job queue pattern)

### Variables & Secrets

- **Workspace variables**: `category=terraform` → `TF_VAR_{key}` env var; `category=env` → raw env var
- **Sensitive variables**: Fernet-encrypted (AES-128-CBC + HMAC-SHA256) in `encrypted_value` column; `value` column empty; never returned in API responses
- **State files**: Fernet-encrypted at rest in object storage. Encrypted bytes use a `TPENC1:` magic prefix so legacy plaintext state is read transparently (no migration needed). Encryption is a no-op if `TERRAPOD_ENCRYPTION__KEY` is unset (dev mode)
- **Variable sets**: Org-scoped, applicable to multiple workspaces (global or assigned)
- **Precedence** (highest wins): Priority variable set vars → Workspace variables → Non-priority variable set vars
- **Encryption key**: `TERRAPOD_ENCRYPTION__KEY` env var (Fernet key); no key = sensitive variables rejected, state stored unencrypted, VCS connections rejected
- **Injection**: Listener resolves all variables → injects into K8s Job env vars

### CSP Identity

- Per-pool `service_account_name` column in `agent_pools` table
- Runner Jobs use `serviceAccountName` from the pool config
- Supports AWS IRSA, Azure Workload Identity, GCP WIF via SA annotations
- Terrapod doesn't manage the SA or annotations — just references them

## Registry & Caching Architecture

Three related artifact management features share the existing storage layer.

### Private Module Registry

- **DB models**: `registry_modules` → `registry_module_versions` (1:N)
- **Storage**: Tarballs at `registry/modules/{org}/{ns}/{name}/{provider}/{version}.tar.gz`
- **Two API surfaces**: CLI protocol (`/api/v2/registry/modules/...`) for `terraform init` + TFE V2 JSON:API (`/api/v2/organizations/{org}/registry-modules/...`) for management
- **Upload flow**: Create version → get presigned PUT URL → client uploads tarball → confirm

### Module Caching (Pull-Through)

- **Pull-through cache**: Same pattern as provider caching — on first request for a public module, fetches from upstream (`registry.terraform.io`), stores tarball in object storage, serves from cache on subsequent requests
- **DB model**: `cached_modules` tracks cached module versions with hostname, namespace, name, provider, version
- **Storage**: Cached tarballs at `cache/modules/{hostname}/{namespace}/{name}/{provider}/{version}.tar.gz` (separate path from private modules)
- **Endpoints**: Implements the Terraform module registry protocol for upstream modules; no ambiguity with private modules — private sources use the Terrapod hostname (e.g. `terrapod.local/org/module/provider`) while cached upstream sources retain their original hostname (e.g. `registry.terraform.io/hashicorp/consul/aws`)
- **Config**: `registry.module_cache.enabled`, `upstream_registries`
- **Runner integration**: Runners resolve modules through the Terrapod API registry, which serves private modules directly and proxies/caches public module references

### Private Provider Registry

- **DB models**: `registry_providers` → `registry_provider_versions` → `registry_provider_platforms` (1:N:N), plus `gpg_keys` for signing
- **Storage**: Binaries at `registry/providers/{org}/{ns}/{name}/{version}/{name}_{version}_{os}_{arch}.zip`, plus `SHA256SUMS` and `SHA256SUMS.sig`
- **GPG keys**: Parsed via `pgpy` (pure Python, no gpg binary); key_id extracted from ASCII armor at creation
- **Two API surfaces**: CLI protocol (`/api/v2/registry/providers/...`) + TFE V2 JSON:API management + GPG key CRUD at `/api/registry/private/v2/gpg-keys`

### Provider Caching (Network Mirror)

- **Pull-through cache**: On first request, fetches from upstream (`registry.terraform.io`), stores in object storage, serves from cache
- **DB model**: `cached_provider_packages` tracks cached binaries with hostname, namespace, type, version, os, arch
- **Endpoints**: `/v1/providers/{hostname}/{namespace}/{type}/index.json` (version list) and `/{version}.json` (platform archives with `zh:` hashes)
- **Config**: `registry.provider_cache.enabled`, `upstream_registries`, `warm_on_first_request`
- **Runner integration**: Runner Jobs must be configured to use the Terrapod API as their network mirror via `TF_CLI_CONFIG_FILE` env var pointing at a generated config with `provider_installation { network_mirror { url = "..." } }`. Both `terraform` and `tofu` respect `TF_CLI_CONFIG_FILE`, so a single config file works for either backend. Runners should never fetch providers directly from upstream registries

### Binary Caching (Terraform/Tofu CLI)

- **Pull-through cache**: Downloads from `releases.hashicorp.com` (terraform) or GitHub releases (tofu) on first use
- **DB model**: `cached_binaries` tracks tool, version, os, arch, shasum
- **Endpoint**: `GET /api/v2/binary-cache/{tool}/{version}/{os}/{arch}` → 302 redirect to presigned URL
- **Admin endpoints**: list, warm (pre-cache), purge
- **Runner image change**: `runners.image` now defaults to `ghcr.io/mattrobinsonsre/terrapod-runner` (generic image with no baked-in binary); runner Job fetches exact version at startup from binary cache
- **Runner provider path**: Runner Jobs fetch the terraform/tofu binary from the binary cache, then use the provider cache (network mirror) for all provider downloads — both caching layers sit in the Terrapod API so runners have zero direct upstream dependencies

### Cache Expiry

All three caching layers (module cache, provider cache, binary cache) use a configurable TTL. Cached entries older than the TTL are eligible for eviction. On the next request for an expired entry, the API re-fetches from upstream and replaces the cached copy.

- **Default TTL**: 30 days
- **Config**: `registry.cache_ttl_days` (applies to all cache layers)
- **Helm**: `api.config.registry.cache_ttl_days` in `values.yaml`
- **DB**: `cached_at` timestamp on each cache record is compared against the TTL at request time
- **Eviction**: Background reaper runs on a configurable schedule (default: daily) and deletes cached entries where `cached_at + TTL < now()` from both the database and object storage. On next request, a cache miss triggers a fresh pull-through fetch from upstream

### Service Discovery

`/.well-known/terraform.json` includes `modules.v1` and `providers.v1` paths pointing to the registry endpoints, enabling `terraform init` with private module/provider sources. The module registry serves private modules under the Terrapod hostname; the module cache proxies upstream registries transparently.

## VCS Integration Architecture

**Polling-first, webhooks optional.** Many self-hosted deployments sit behind firewalls where VCS providers can't push webhooks. Terrapod polls VCS providers for changes (outbound HTTPS only). When webhooks are configured (GitHub only, currently), they trigger an immediate poll cycle for faster feedback.

### VCS Connections

A **VCS connection** is a platform-level resource that configures auth for a VCS provider. Created by an admin via the API, each connection is specific to a provider:

- **GitHub**: uses a GitHub App installation. App ID + PEM private key + installation ID stored on the connection (`token_encrypted` for the private key, Fernet-encrypted). Auth via RS256-signed JWT → installation access token (cached 50 min).
- **GitLab**: uses a Project or Group Access Token. Token is Fernet-encrypted at rest (`token_encrypted` column). Supports GitLab.com and self-hosted via `server_url`.

Both providers are fully dynamic — all credentials live on the VCS connection itself. No secrets in Helm values or platform config. The only platform-level settings are `vcs.enabled`, `vcs.poll_interval_seconds`, and optionally `vcs.github.webhook_secret` for webhook HMAC validation.

- **DB model**: `VCSConnection` — id, org_name, provider ("github"/"gitlab"), name, server_url, token_encrypted (PEM key or PAT, Fernet-encrypted), github_app_id, github_installation_id, github_account_login, github_account_type, status
- **API**: `GET/POST /api/v2/organizations/{org}/vcs-connections`, `GET/DELETE /api/v2/vcs-connections/{id}`

### Provider Dispatch

The `VCSProvider` protocol (`vcs_provider.py`) defines the interface for VCS operations. The poller dispatches to `github_service` or `gitlab_service` based on `conn.provider`. Each provider implements: `get_branch_sha()`, `get_default_branch()`, `download_archive()`, `list_open_prs()`, `parse_repo_url()`. All functions take `VCSConnection` as the first argument and derive auth from it.

### GitHub App Authentication

- **GitHub App** provides auth — installation tokens for repo access, scoped permissions, no user-level OAuth tokens
- App ID, private key (PEM), and installation ID stored on the `VCSConnection` (private key Fernet-encrypted in `token_encrypted`)
- `server_url` on the connection determines the GitHub API URL (default: `https://api.github.com`, change for GHE)
- **JWT generation**: RS256-signed JWT from app private key (10-min lifetime, PyJWT)
- **Installation tokens**: cached for 50 minutes (valid 60 min), used for all GitHub API calls

### GitLab Token Authentication

- **Access Token** (Project or Group) with `read_api` + `read_repository` scopes
- Token stored Fernet-encrypted on the `VCSConnection` record (same encryption as sensitive variables)
- `server_url` on the connection determines the GitLab instance (default: `https://gitlab.com`)

### Workspace VCS Configuration

Each workspace has optional VCS fields:
- `vcs_connection_id` — FK to `vcs_connections`, determines which provider + auth to use
- `vcs_repo_url` — git URI (e.g. `https://github.com/org/repo` or `https://gitlab.com/group/project`)
- `vcs_branch` — branch to track (empty = repo default branch)
- `vcs_working_directory` — subdirectory within repo (empty = root)
- `vcs_last_commit_sha` — last polled HEAD SHA (internal tracking)

### VCS Poller (Background Task)

- Runs as an async task in the API server's lifespan (when `vcs.enabled=true`)
- Every `poll_interval_seconds` (default 60), iterates workspaces with `vcs_repo_url` and `vcs_connection_id` set
- Provider-agnostic: dispatches to GitHub or GitLab based on `conn.provider`
- For each workspace, two checks per cycle:
  1. **Branch push**: calls `get_branch_sha()`, compares to `vcs_last_commit_sha`. On change: downloads tarball, creates ConfigurationVersion (`source="vcs"`), queues a full plan/apply Run
  2. **Open PRs/MRs**: calls `list_open_prs()` for PRs/MRs targeting the tracked branch. For each with a new head SHA (no existing run for that PR+SHA combo): downloads tarball, creates speculative (plan-only) Run
- Webhook handler (`POST /api/v2/vcs-events/github`) validates HMAC-SHA256 and calls `trigger_immediate_poll()` for faster feedback

### VCS-Driven Run Flow

```
Push to tracked branch → Poller detects new SHA → Download tarball → ConfigurationVersion → Queue Run (plan + apply)

PR/MR opened/updated → Poller detects new head SHA → Download tarball → ConfigurationVersion → Queue Run (plan-only, speculative)
                          ↑
Webhook (optional, GitHub only) → trigger_immediate_poll() → Poller runs early for either case
```

### PR/MR Deduplication

The poller avoids creating duplicate speculative runs by checking if a run already exists for the same workspace + PR/MR number + head SHA. Once a run exists (in any state), no new run is created until the head SHA changes (i.e. a new commit is pushed).

### Run VCS Metadata

Runs created by the VCS poller carry metadata:
- `vcs_commit_sha` — the commit that triggered the run
- `vcs_branch` — the branch name (tracked branch for pushes, head ref for PRs/MRs)
- `vcs_pull_request_number` — PR/MR number (null for branch push runs)

## Workspace & State Architecture

### Execution Modes

- **Local** (implemented): `terraform`/`tofu` runs plan/apply locally, pushes state to Terrapod. The `cloud` block in HCL points at the Terrapod API as a remote state backend with workspace locking.
- **Remote** (target): The server runs plan/apply via the runner infrastructure. Requires the full plan/apply workflow (runs, configuration versions, log streaming).

### State Version Upload Flow

The TFE V2 API uses a two-step state upload (matching HCP Terraform's presigned URL pattern):

1. `POST /api/v2/workspaces/{id}/state-versions` — create state version record (serial, md5, lineage)
2. `PUT /api/v2/state-versions/{id}/content` — upload raw state bytes (no auth — go-tfe uses presigned-style uploads without `Authorization` header; the state version UUID acts as a capability token)
3. Optionally `PUT /api/v2/state-versions/{id}/json-content` — upload JSON state outputs (accepted and discarded for now)

### State Locking

Workspace locking uses `POST /api/v2/workspaces/{id}/actions/lock` and `POST .../actions/unlock`. The lock ID from the client request body is stored in the workspace row. Lock conflicts return 409.

### go-tfe Compatibility Notes

- go-tfe v1.95.0 (used by OpenTofu 1.11.x) does NOT send `Authorization` headers on state upload PUT requests — upload endpoints must not require auth
- Timestamps must be RFC3339 with `Z` suffix (not `+00:00`)
- Response content-type can be `application/json` (go-tfe doesn't check for `application/vnd.api+json`)
- Upload URLs must be absolute (go-tfe doesn't resolve relative URLs for raw requests)

## Tech Stack

Following the patterns established in [bamf](~/code/bamf):

| Layer | Technology | Rationale |
|---|---|---|
| API server | **Python / FastAPI** | Async, productive for JSON:API CRUD; matches bamf patterns (SQLAlchemy, Pydantic, Alembic) |
| Database | **PostgreSQL** | Proven, open-source, rich ecosystem |
| Object storage | **Native SDKs** (boto3, azure-storage-blob, google-cloud-storage) + filesystem fallback | AWS S3, Azure Blob, GCS natively; local filesystem for dev. No S3 compat shim, no MinIO |
| Task queue | **PostgreSQL-based** (e.g. [procrastinate](https://procrastinate.readthedocs.io/) or [SAQ](https://github.com/tobymao/saq)) | Avoid extra infra; Postgres or Redis as job queue |
| Frontend | **Next.js 15 + React 19 + TypeScript + Tailwind CSS + Radix UI** | Matches bamf; modern, fast, accessible |
| Runner listener | **Python** | Same codebase as API; creates ephemeral K8s Jobs, streams logs via `kubernetes` Python client |
| Policy engine | **OPA** | Open-source, CNCF-graduated, Rego language |
| Cost estimation | **Infracost** | Open-source, actively maintained |
| Auth | **authlib** (OIDC / SAML) | Same library as bamf; pluggable identity |

### Why All Python?

- **Python (FastAPI)** for the API server — JSON:API serialization, CRUD, auth, webhooks, VCS integration. This is string-heavy web application work where Python excels and iteration speed matters.
- **Python for the runner listener** — same codebase as the API server, shared Pydantic models and config, single Docker image with different entrypoints. The `kubernetes` Python client is mature enough for creating Jobs and streaming logs. One language means one CI pipeline, one set of dependencies, and simpler operations.
- **No Go** — originally planned for the execution worker, but the operational overhead of a second language (separate CI, build tooling, module management) outweighs the marginal performance benefit. Python's async capabilities are sufficient for the K8s Job lifecycle.

## Project Structure

```
terrapod/
  services/
    pyproject.toml          # Poetry config (Python deps, ruff, mypy, pytest)
    terrapod/              # Python API server (FastAPI)
      api/
        app.py              # FastAPI app factory with lifespan
        health.py           # Health check endpoints (DB + Redis readiness)
        dependencies.py     # Unified auth dependency (session + API token)
        routers/
          auth.py           # Auth router: providers, authorize, callback, token, sessions, logout
          oauth.py          # Terraform CLI login: /.well-known/terraform.json, /oauth/authorize, /oauth/token
          tfe_v2.py         # TFE V2 compat: ping, account, orgs, workspaces, state versions, lock/unlock
          tokens.py         # TFE V2 token CRUD: create, list, show, delete (JSON:API)
          registry_modules.py    # Module registry: CLI protocol + TFE V2 management (JSON:API)
          registry_providers.py  # Provider registry: CLI protocol + TFE V2 management (JSON:API)
          gpg_keys.py       # GPG key CRUD for provider signing (/api/registry/private/v2/gpg-keys)
          binary_cache.py   # Terraform/tofu binary cache for runners + admin endpoints
          module_mirror.py  # Module registry proxy for upstream module caching
          provider_mirror.py # Network mirror protocol for upstream provider caching
          variables.py      # Variable + varset CRUD (TFE V2 compatible)
          agent_pools.py    # Pool CRUD + join/heartbeat/renew endpoints
          runs.py           # Run CRUD + confirm/discard/cancel + queue polling
          config_versions.py # Configuration version upload endpoints
          vcs_connections.py   # VCS connection CRUD (admin, JSON:API)
          vcs_events.py        # GitHub webhook receiver (optional, HMAC-validated)
          roles.py             # Role CRUD (admin only): list, create, show, update, delete
          role_assignments.py  # Role assignment management (admin only): list, set, delete
      auth/
        sso.py              # SSOConnector ABC, AuthorizationRequest, AuthenticatedIdentity
        ca.py               # Certificate Authority (Ed25519, listener certificates)
        sessions.py         # Redis-backed session management (sliding 12h TTL)
        auth_state.py       # Ephemeral Redis auth state (AuthState, AuthCode)
        passwords.py        # PBKDF2-SHA256 hashing + zxcvbn strength validation
        claims_mapper.py    # IDP claims-to-roles mapping
        builtin_roles.py    # admin, audit, everyone built-in roles
        recent_users.py     # Redis-backed recent user tracking for admin UX
        api_tokens.py       # Long-lived API tokens (SHA-256 hashed in PostgreSQL)
        connectors/
          __init__.py        # Connector registry (init, get, list, default)
          local.py           # Local password auth connector
          oidc.py            # OIDC connector (authlib)
          saml.py            # SAML connector (python3-saml)
      db/
        session.py          # SQLAlchemy async session factory
        models.py           # User, Role, RoleAssignment, PlatformRoleAssignment, APIToken,
                            #   AgentPool, AgentPoolToken, RunnerListener,
                            #   RegistryModule, RegistryModuleVersion, RegistryProvider,
                            #   RegistryProviderVersion, RegistryProviderPlatform, GPGKey,
                            #   CachedModule, CachedProviderPackage, CachedBinary,
                            #   Workspace (incl. labels, owner_email, vcs_repo_url, vcs_branch, vcs_connection_id), StateVersion,
                            #   CertificateAuthorityModel, VCSConnection,
                            #   Variable, VariableSet, VariableSetVariable, VariableSetWorkspace,
                            #   ConfigurationVersion, Run (incl. vcs_commit_sha, vcs_branch)
      runner/               # Runner listener (ARC-pattern Job controller)
        __init__.py
        __main__.py         # python -m terrapod.runner.listener entry point
        listener.py         # Main listener loop with poll/heartbeat/execute
        identity.py         # Local vs remote identity establishment
        job_manager.py      # K8s Job CRUD, watching, log retrieval
        job_template.py     # Build Job spec from runner definition + run params
      redis/
        client.py           # Async Redis client (init/close/get)
      services/
        sso_service.py      # Login processing with role resolution from 3 sources
        rbac_service.py     # Label-based RBAC evaluation (allow/deny labels and names)
        workspace_rbac_service.py    # Workspace permission resolution (read/plan/write/admin hierarchy)
        registry_rbac_service.py     # Registry permission resolution (read/write/admin hierarchy)
        registry_module_service.py   # Module registry CRUD + presigned URL generation
        registry_provider_service.py # Provider registry CRUD + download info assembly
        gpg_key_service.py  # GPG key CRUD + PGP parsing via pgpy
        binary_cache_service.py      # Pull-through terraform/tofu binary cache
        module_cache_service.py      # Pull-through module cache (upstream proxy)
        provider_cache_service.py    # Pull-through provider binary cache (network mirror)
        encryption_service.py        # Fernet encrypt/decrypt for sensitive variables, VCS tokens, state files
        variable_service.py          # Variable CRUD + resolution with precedence
        run_service.py               # Run state machine + creation + queue
        agent_pool_service.py        # Pool/token/listener management + join flow
        github_service.py            # GitHub App JWT, installation tokens, repo operations
        gitlab_service.py            # GitLab access token auth, repo operations
        vcs_provider.py              # VCSProvider protocol + PullRequest model
        vcs_poller.py                # Background polling loop for VCS-driven runs (provider-agnostic)
      storage/
        __init__.py         # Factory, DI (init_storage/close_storage/get_storage)
        protocol.py         # ObjectStore Protocol, ObjectMeta, PresignedURL, exceptions
        keys.py             # Key path helpers (state/, config/, plans/, logs/, registry/, cache/)
        filesystem.py       # Local filesystem with HMAC-signed URLs
        filesystem_routes.py  # FastAPI endpoints for filesystem presigned URL handling
        s3.py               # AWS S3 via aioboto3
        azure.py            # Azure Blob via azure-storage-blob (async)
        gcs.py              # GCS via google-cloud-storage + gcloud-aio-storage
      cli/
        __init__.py
        bootstrap.py        # Idempotent admin user bootstrap (Helm post-install hook)
      config.py             # Pydantic Settings (YAML + env vars, auth, SSO, storage, runners, registry)
      logging_config.py     # structlog setup
    tests/                  # pytest test suite
      auth/                 # Auth unit tests (passwords, sessions, state, claims, RBAC, tokens)
      api/                  # API tests (oauth, tfe_v2, auth dependency)
      storage/              # Storage backend tests (conformance, unit, integration)
  alembic/
    env.py                  # Async Alembic environment
    versions/
      001_initial_auth.py   # User, Role, RoleAssignment, PlatformRoleAssignment, APIToken
      002_remove_token_expiry.py  # Remove expired_at from api_tokens
      003_agent_pools_and_listeners.py  # AgentPool, AgentPoolToken, RunnerListener + default pool seed
      004_registry.py     # RegistryModule, RegistryModuleVersion, RegistryProvider,
                          #   RegistryProviderVersion, RegistryProviderPlatform, GPGKey
      005_caching.py      # CachedProviderPackage, CachedBinary
      006_workspaces.py   # Workspace, StateVersion
      007_runner_infrastructure.py  # CertificateAuthority, Variable, VariableSet,
                                    #   VariableSetVariable, VariableSetWorkspace,
                                    #   ConfigurationVersion, Run + workspace/pool alterations
      008_workspace_resources.py    # Per-workspace resource_cpu/resource_memory, drop runner_definition
      009_module_cache.py            # CachedModule table for pull-through module caching
      010_vcs_integration.py         # VCSConnection table, workspace VCS columns, run VCS metadata
      011_rbac_enforcement.py        # Workspace labels/owner_email, Role workspace_permission, Registry labels/owner_email
      012_remove_api_token_team_id.py # Drop unused team_id column from api_tokens
  web/                      # Next.js 15 frontend (React 19, Tailwind, Radix UI)
    package.json            # Next.js 15, React 19, Radix, Tailwind, lucide-react
    next.config.js          # standalone output + /api/* rewrite to localhost:8001
    tailwind.config.ts      # dark mode, brand purple palette, glow shadows
    tsconfig.json           # strict, @/* → ./src/*
    src/
      app/
        globals.css         # Tailwind directives + btn-smoke glow effect
        layout.tsx          # Server component: Inter font, dark html, metadata
        page.tsx            # Dashboard: user info, quick links
        login/page.tsx      # Login: provider list, local form, SSO buttons
        auth/callback/      # PKCE token exchange (page.tsx + callback-handler.tsx)
        registry/
          modules/page.tsx  # Module list + create
          modules/[org]/[namespace]/[name]/[provider]/page.tsx  # Module detail
          providers/page.tsx # Provider list + create
          providers/[org]/[namespace]/[name]/page.tsx           # Provider detail
        workspaces/
          page.tsx          # Workspace list + create
          [id]/page.tsx     # Workspace detail (tabs: overview, variables, runs, state)
        settings/
          tokens/page.tsx   # API token CRUD
          sessions/page.tsx # Active sessions
        admin/
          binary-cache/page.tsx  # Binary cache admin (admin-only)
      components/
        nav-bar.tsx         # Sticky top nav, responsive, admin-gated links
        session-expiry-banner.tsx  # Expiry warning + auto-redirect
        page-header.tsx     # Title + description + actions slot
        empty-state.tsx     # Empty list message
        error-banner.tsx    # Red error display
        loading-spinner.tsx # Spinning loader
      lib/
        auth.ts             # sessionStorage auth state (token, email, roles, userId)
        api.ts              # Authenticated fetch wrapper (auto 401 → login redirect)
        pkce.ts             # PKCE challenge generation
  helm/
    terrapod/              # Helm chart
      Chart.yaml
      values.yaml           # Production defaults (auth, postgresql, redis)
      values-local.yaml     # Tilt local dev overrides
      templates/
        _helpers.tpl         # Template helpers + storage validation
        configmap-api.yaml   # API config.yaml ConfigMap (includes auth config)
        configmap-runner.yaml # Runner definitions ConfigMap (runners.yaml)
        deployment-api.yaml  # API Deployment (DB + Redis env vars)
        deployment-listener.yaml  # Runner listener Deployment (same image, different entrypoint)
        rbac-listener.yaml   # Listener SA, Role, RoleBinding (Jobs + Pods in runner namespace)
        service-api.yaml     # API Service
        serviceaccount.yaml  # ServiceAccount (with cloud identity annotations)
        deployment-web.yaml   # Web UI Deployment (Next.js)
        service-web.yaml     # Web UI Service
        ingress.yaml         # BFF Ingress (all traffic → web frontend)
        job-migrations.yaml  # Alembic migrations (pre-install/pre-upgrade hook)
        job-bootstrap.yaml   # Admin user bootstrap (post-install hook)
        pvc-storage.yaml     # PVC for filesystem backend
        pdb-api.yaml         # PodDisruptionBudget
  docker/
    Dockerfile.api          # Production API image (includes xmlsec1 for SAML)
    Dockerfile.web          # Production web image (Next.js standalone) + Tilt dev target
    Dockerfile.runner       # Minimal runner Job image (Alpine + curl/tar/jq, signal-forwarding entrypoint)
    Dockerfile.test         # Test/lint runner image (includes xmlsec1 for SAML)
    runner-entrypoint.sh    # Signal-forwarding entrypoint for graceful terraform/tofu shutdown
  docker-compose.test.yml   # Containerised test runner (LocalStack, PostgreSQL, Redis)
  scripts/
    lib.sh                  # Shared CI helpers
    lint.sh                 # Containerised linting
    test.sh                 # Containerised testing
  Makefile                  # Thin wrapper around scripts/
  Tiltfile                  # Local K8s development (ns: terrapod, port: 10352, PG + Redis)
  .github/workflows/ci.yml # GitHub Actions CI
  CLAUDE.md
```

## Development Conventions

Following bamf patterns:

- **API + Runner listener**: Python 3.13+, FastAPI, SQLAlchemy (async), Pydantic, structlog, kubernetes client
- **Frontend**: Next.js 15, React 19, TypeScript, Tailwind CSS, Radix UI, TanStack Query
- **Package managers**: Poetry (Python), npm (frontend)
- **Build**: `make` → `scripts/*.sh`; **all builds, tests, and linting run in Docker** — no local Poetry/pip install needed
- **Testing**: pytest (API + runner), Jest/Vitest (frontend) — all containerised via `docker-compose.test.yml`
- **Linting**: ruff + mypy (Python), eslint + tsc (frontend)
- **Database migrations**: Alembic with async SQLAlchemy
- **API contract**: JSON:API spec; compatibility tested against `go-tfe` client library
- **Commits**: conventional commits (`feat:`, `fix:`, `docs:`, `chore:`, etc.)
- **Branches**: feature branches off `main`; never push directly to `main`
- **CI**: GitHub Actions; containerised jobs (Python, Node, Helm); gate job aggregates results
- **Docker**: multi-stage builds; non-root users; health checks
- **Local dev**: Tilt (K8s-first); live_update for Python and Node

## Local Development

Tilt is the primary local development tool. All builds and tests run in Docker. The frontend runs in K8s alongside the API — not as a local process.

### Prerequisites

```zsh
brew install mkcert && mkcert -install  # Local TLS CA
sudo sh -c 'echo "127.0.0.1 terrapod.local" >> /etc/hosts'
```

### Tilt setup

| Setting | Value |
|---|---|
| **Namespace** | `terrapod` |
| **Tilt UI port** | `10352` |
| **URL** | `https://terrapod.local` |

These are deliberately different from bamf (`bamf` ns, port 10350) and kubamf (`kubamf` ns, port 10350) so all three can run simultaneously.

### Architecture (BFF pattern)

All traffic enters through the Next.js frontend via a single Ingress on `terrapod.local`. Next.js rewrites proxy `/api/*` and `/.well-known/*` to the API service internally (via `API_URL` env var pointing at `http://terrapod-api:8000`). The browser never talks to the API directly.

Tilt builds the web Docker image targeting the `builder` stage and runs `next dev` for hot reload. Source changes in `web/src/` are synced via `live_update` — no image rebuild needed.

```zsh
make dev          # starts tilt on port 10352
make dev-down     # stops tilt
```

### Running tests and linting

```zsh
make test         # pytest in Docker (with LocalStack for S3)
make lint         # ruff check + format --check in Docker
make test-down    # tear down test containers
```

### Docker images

```zsh
make images       # builds terrapod-api:local and terrapod-web:local
```

## Existing Open-Source Landscape

Understanding what already exists to avoid reinventing and to identify collaboration opportunities:

| Project | What it does | Gap vs full TFE replacement |
|---|---|---|
| [OpenTofu](https://opentofu.org/) | Open-source Terraform fork (CLI engine) | CLI only — no collaboration platform. Terrapod uses it as an execution backend |
| [Atlantis](https://www.runatlantis.io/) | PR-based plan/apply automation | No UI, no state management, no registry, no RBAC beyond repo permissions |
| [Digger](https://digger.dev/) | CI-native Terraform orchestration | Runs inside your CI (GitHub Actions); no standalone platform |
| [Terrateam](https://terrateam.io/) | GitHub-integrated TF automation | GitHub-coupled; community edition is limited |
| [Spacelift](https://spacelift.io/) | Commercial TF management platform | Not open source |

**Terrapod's differentiator**: a single, self-hosted platform that covers the full TFE surface (state + runs + registry + policy + UI + API) under a permissive open-source license.

## Maintaining This File

Claude is responsible for keeping this CLAUDE.md up to date as the project evolves. When architectural decisions are made, conventions change, new patterns are established, or scope shifts during conversation, Claude must update this file to reflect the current state of the project. This file is the single source of truth for project context across sessions.

## License

Apache 2.0 — maximally permissive for enterprise adoption.
