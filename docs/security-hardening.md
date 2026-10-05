# Security Hardening Guide

This guide covers production security hardening for Terrapod deployments. It assumes you have a working Terrapod installation and want to tighten its security posture.

## TLS Configuration

### Database (PostgreSQL)

Use `sslmode=verify-full` in the database connection URL to enforce TLS with certificate validation:

```yaml
postgresql:
  url: "postgresql+asyncpg://terrapod:password@db.example.com:5432/terrapod?ssl=verify-full"
```

For managed databases (RDS, Cloud SQL, Azure Database), enable SSL enforcement at the provider level and provide the CA certificate bundle.

### Redis

Use `rediss://` (note the double `s`) to enforce TLS on Redis connections:

```yaml
redis:
  url: "rediss://default:password@redis.example.com:6380"
```

For ElastiCache, MemoryDB, or Azure Cache for Redis, enable in-transit encryption at the provider level.

### Ingress

Always terminate TLS at the ingress controller. Provide a valid certificate:

```yaml
ingress:
  enabled: true
  hostname: terrapod.example.com
  tls: true
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
```

## Authentication Hardening

### Disable Local Authentication

When an SSO provider (OIDC/SAML) is configured, disable local password authentication to enforce centralized identity management:

```yaml
api:
  config:
    auth:
      local_enabled: false
      sso:
        default_provider: "your-idp"
```

### API Token Lifetime

Reduce the maximum API token lifetime. The default is 8760 hours (1 year); `0` means no limit. For stricter environments:

```yaml
api:
  config:
    auth:
      api_token_max_ttl_hours: 24  # Tokens expire after 24 hours
```

### Require SSO for Privileged Roles

Force specific roles to authenticate via external SSO (not local passwords):

```yaml
api:
  config:
    auth:
      require_external_sso_for_roles:
        - admin
        - audit
```

### Validate SAML Assertions Strictly

If you use a SAML provider, confirm all five assertion checks are on. They are
per provider, and on the 1.x release lines they default to **off** so that a
patch release could not lock anyone out — which means a deployment carried
forward from 1.x keeps the permissive setting until you say otherwise:

```yaml
api:
  config:
    auth:
      sso:
        saml:
          - name: azure-ad
            metadata_url: "https://login.microsoftonline.com/…/federationmetadata.xml"
            validate_destination: true          # the assertion is addressed to us
            validate_in_response_to: true       # it answers a request we sent
            reject_replayed_assertions: true    # it is used once
            want_assertions_signed: true        # the claims are covered by a signature
            reject_deprecated_algorithm: true   # no SHA-1
```

`validate_destination` is the one to turn on first: without it an assertion the
IDP issued for a different service provider is accepted here, so anyone who can
obtain one for a host they control can replay it at Terrapod and log in as that
user. See [Authentication](authentication.md#assertion-validation) for what each
check refuses and how to read a failure.

## Secrets Management

### Use Kubernetes Secrets

Never put credentials directly in `values.yaml`. Use `existingSecret` references:

```yaml
postgresql:
  existingSecret: "terrapod-db-credentials"
  existingSecretKey: "url"

redis:
  existingSecret: "terrapod-redis-credentials"
  existingSecretKey: "url"
```

The same applies to the listener's join token (`listener.existingSecret`), the
bootstrap admin password (`bootstrap.existingSecret`) and each bootstrapped
pool's join token (`bootstrap.pools[].existingSecret`).

**What the chart does when you supply one in values anyway.** It puts it in a
chart-managed Secret and references that, rather than rendering it into the
manifest — so the database URL, the listener join token and the bootstrap admin
password and pool token never appear in a Deployment or Job spec, where anyone
with `get` on those objects could read them. That is a floor, not a substitute
for `existingSecret`: a value given in `values.yaml` still passes through the
release's own stored values, which `helm get values` returns and which a GitOps
repository, CI log or Terraform state file may hold. Only an `existingSecret`
keeps it out of that path.

### Bootstrap join tokens expire

A bootstrap join token is created with **one use and a 24-hour lifetime** by
default. A listener holding a valid join token can register itself in the pool,
and a listener in the pool claims runs and receives every variable those runs
resolve — so a permanent, unlimited one is a standing credential for reading
every workspace's secrets.

One use is enough for what the token is for. A listener keeps its issued
certificate in a Secret in its own namespace, so restarts, rolling updates and
scale-out all adopt the existing identity and never touch the join token; and
when several replicas start together the one that wins writes the Secret while
the others adopt it.

```yaml
bootstrap:
  poolTokenMaxUses: 1        # 0 = unlimited
  poolTokenTTLSeconds: 86400 # 24h. 0 = never expires
```

Raise the use limit if you want tolerance for one particular race: the first
listener pod consumes the single use, and if it dies between joining and writing
its credentials Secret, nothing else can take over. The API's own default for
tokens created through it is 2, for exactly that reason.

An expired or spent token is **not** re-armed by a later `helm upgrade` — doing
so would be the same permanent credential by another route. Issue a replacement
through the API (`POST /api/v1/agent-pools/{pool_id}/tokens`) if a listener needs
to join after that.

For SSO provider client secrets, create a K8s Secret and reference it:

```bash
kubectl create secret generic terrapod-oidc \
  --from-literal=client_secret=<your-secret>
```

```yaml
api:
  config:
    auth:
      sso:
        oidc:
          - name: "your-idp"
            existingSecret: "terrapod-oidc"
            existingSecretKey: "client_secret"
```

### External Secrets Operator

For production, use [External Secrets Operator](https://external-secrets.io/) to sync secrets from AWS Secrets Manager, Azure Key Vault, or GCP Secret Manager into Kubernetes Secrets automatically.

### Token signing key

One key signs four families of stateless token — runner tokens, run-task
callback tokens, download tickets and Slack link tokens.

**Terrapod generates it and stores it in the database, and there is nothing to
configure.** It is created on first startup, read on every startup, and the
check-then-create is serialized across replicas with a Postgres advisory lock —
the same mechanism, for the same reasons, as the listener certificate authority.
Replicas cannot disagree about a key they all load from one row.

Two earlier mechanisms are gone, and it is worth knowing why if you are
upgrading:

* It was **derived from `sha256(database_url)`** when nothing else was
  configured, which is the wrong material for the job: a database URL is a
  credential for another system, handed to every client of that database, to
  backup and migration jobs, and to a DBA who has no business forging platform
  tokens.
* It was then **generated by the chart**, which put a random value in a rendered
  manifest. That worked under `helm install` and was actively dangerous under a
  renderer — see the note on upgrading below.

#### If you upgraded from before the key was stored

A deployment that arrives with no configured key and has executed runs
**adopts** its previous `sha256(database_url)` value rather than minting a new
one. That is deliberate: a different key invalidates every token already in
flight, and those tokens are what running plans and applies authenticate with.
An apply that succeeds and then cannot upload its state leaves the workspace
flagged as diverged, so this is not a cosmetic failure.

The adopted key is still derivable by anyone holding the database URL, so
**startup warns until you replace it**. Replace it by supplying your own
(below); doing so invalidates tokens minted under the old key, so do it when
nothing is mid-apply. Set `api.config.require_strong_secrets` to make that
warning fatal instead of advisory.

A deployment that has never executed a run has nothing in flight, so it
generates a strong key immediately and warns about nothing.

#### Supplying your own key

Optional. Do it when your own secret management is the system of record for key
material — otherwise Terrapod's own key is strong and needs no attention.

```zsh
kubectl -n terrapod create secret generic terrapod-token-signing \
  --from-literal=token_signing_key="$(openssl rand -hex 32)"
```

```yaml
api:
  tokenSigningKey:
    existingSecret: "terrapod-token-signing"
    existingSecretKey: "token_signing_key"
```

It is injected as `TERRAPOD_TOKEN_SIGNING_KEY` from a Secret, never a ConfigMap.
**A supplied key wins on every startup and Terrapod does not read its stored key
at all**, so rotating your Secret takes effect on the next restart. Terrapod
deliberately does not copy your key into its own table: doing so would make the
first value you ever supplied win for ever and silently ignore every later
change.

Rotating a supplied key has to roll every API pod at once. Environment from a
`secretKeyRef` is captured when a pod starts and never refreshes, so a fleet that
disagrees about the key is not a clean failure — it serves `401` on whatever
share of requests lands on a pod holding the other one, which for a run means
roughly half its API calls fail while the rest succeed. Under `helm
install`/`helm upgrade` the chart handles it: the pod template carries a checksum
over the Secret, so a change rolls the Deployment.

#### Under a GitOps controller

**Nothing special is required any more.** The chart no longer creates a Secret
for this key, so there is nothing for a renderer to re-mint and nothing for a
pruning controller to delete. Terrapod's key lives in the database, which
reconciliation does not touch.

This is the problem that removing the generation solved. A rendered manifest has
to be a pure function of its inputs, and a random value is not. Under `helm
install` a cluster `lookup` for the existing Secret covered that; under `helm
template` — what Argo CD and Flux run — `lookup` returns nothing and
`.Release.IsInstall` is always true, so the generating branch minted a fresh key
on every render: a rotation machine pointed at a running deployment. It was
guarded against that, and the guard was the kind of thing that should not need
to exist.

If you do supply your own key, the Secret is yours to own under a GitOps
controller: create it out of band (an External Secrets Operator `ExternalSecret`
from your cloud secret manager, a sealed secret, or whatever your tooling uses)
and keep it outside anything that prunes. The `checksum/token-signing`
annotation that rolls the Deployment is computed by reading the Secret from the
cluster, which a renderer cannot do, so after changing the key **restart the API
yourself**:

```zsh
kubectl -n terrapod rollout restart deploy/<release>-api
```

If a supplied Secret is pruned, pods already running keep the key in their
environment while any pod started afterwards falls back to Terrapod's stored key,
and requests return `401` wherever they land on the wrong half. Recreate the
Secret and roll the API.

## Network Policies

Terrapod ships with NetworkPolicy templates that restrict pod-to-pod and pod-to-external traffic.
**They are off by default** (`networkPolicies.enabled: false`), so nothing below applies to a
deployment that has not set this. Enable them:

```yaml
networkPolicies:
  enabled: true
```

This creates four NetworkPolicies:

| Policy | Ingress | Egress |
|--------|---------|--------|
| **api** | Web, listener, runner on port 8000 | Postgres (5432), Redis (6379), HTTPS (443), DNS |
| **web** | Ingress controller on port 3000 | API (8000), DNS |
| **listener** | None | API (8000), K8s API (443), DNS |
| **runner** | None | API (8000), HTTPS (443), DNS |

**When enabled**, runners are denied access to Postgres and Redis. Note also what the
runner egress list does not contain: port 80. Cloud instance-metadata services listen
there, so a runner cannot reach one — denied by omission rather than by an explicit
rule, which is worth knowing if you audit these policies expecting to find one.

**Enable these together with a separate runner namespace** — see
[Separate the runner namespace](#separate-the-runner-namespace). The two are the
same control from different directions: the namespace bounds what the listener's
Kubernetes grants reach, the policies bound what runner code can connect to.
Either on its own leaves half of it open.

The API policy's runner peer adapts to where the runners are: a bare
`podSelector` when they share the release namespace, and a `namespaceSelector`
scoped to `listener.runnerNamespace` when they do not. That matters because a
bare `podSelector` matches only pods in the policy's own namespace — a detail
that makes a policy look correct while denying every run.

**Prerequisite:** Your cluster must have a CNI plugin that supports NetworkPolicy (Calico, Cilium, Weave Net, etc.).
On a cluster whose CNI does not enforce NetworkPolicy these objects are accepted and
silently ignored, which is why they are not on by default: a policy that is present
but unenforced reads as a control where there is none. Confirm enforcement before
relying on them.

## Pod Security Standards

Use Kubernetes Pod Security Standards to enforce security contexts at the namespace level:

```yaml
namespace:
  create: true
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

Terrapod's default pod and container security contexts are already compatible with the `restricted` profile:
- `runAsNonRoot: true`
- `readOnlyRootFilesystem: true`
- `allowPrivilegeEscalation: false`
- `capabilities.drop: [ALL]`
- `seccompProfile.type: RuntimeDefault`

## Rate Limiting

API rate limiting is enabled by default to protect against brute-force and denial-of-service attacks. The default configuration:

```yaml
api:
  config:
    rate_limit:
      enabled: true
      requests_per_minute: 100
      auth_requests_per_minute: 10
```

Auth endpoints (`/api/v1/auth/*`, `/oauth/*`) have a separate, lower limit to protect against credential stuffing. Health, readiness, and metrics endpoints are exempt.

Rate limiting uses Redis for distributed counting across replicas and fails open if Redis is unavailable.

## Audit Logging

### Retention

Configure audit log retention based on your compliance requirements:

```yaml
api:
  config:
    audit:
      retention_days: 365  # 1 year for SOC2/ISO27001
```

### SIEM Export

Query the audit log API and forward events to your SIEM:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "https://terrapod.example.com/api/v1/admin/audit-log?page[size]=100"
```

Integrate with your log aggregator (Elasticsearch, Splunk, Datadog) by polling this endpoint periodically.

## Database Hardening

- **Encryption at rest**: Enable at the provider level (RDS encryption, Cloud SQL encryption, Azure Database encryption)
- **Network isolation**: Place the database in a private subnet with no public access
- **Credential rotation**: Use IAM database authentication (RDS) or workload identity (Cloud SQL, Azure) instead of static passwords
- **Connection pooling**: Use PgBouncer or a managed connection pool to limit concurrent connections and prevent connection exhaustion

## Runner Isolation

Runner Jobs execute untrusted Terraform/Tofu code. Harden them:

### Separate the runner namespace

**Run runner Jobs in a namespace of their own, and turn NetworkPolicies on. This
is the single most valuable change on this page, and neither is the default.**

```yaml
listener:
  runnerNamespace: terrapod-runners   # anything other than the release namespace

namespace:
  createRunner: true                  # let the chart create it
  runnerLabels:                       # untrusted code runs here
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted

networkPolicies:
  enabled: true
  web:
    ingressFrom:                      # required when policies are on
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: ingress-nginx
```

`listener.runnerNamespace` defaults to the release namespace, so out of the box
runner Jobs run beside the API, the listener and the web pod. Where a workload
runs is your decision and the chart does not move it for you — a default that
relocated runner Jobs out from under an existing deployment's quotas, policies
and node selectors would be a surprise on upgrade. So this is a recommendation
rather than a default, and what follows is what the default costs.

**What you are exposed to while they share a namespace.** Two things, neither
bounded by anything else in the chart:

- **The listener can read every Secret in the namespace.** It holds
  `jobs: create` and `secrets: create` there, which is what it needs to launch
  runs. A compromised listener can use `jobs: create` to start a Job that mounts
  any Secret in the namespace and print it to the pod's own logs — the token
  signing key, the database URL, the OIDC, Slack, Vault and webhook secrets, and
  every per-run variables Secret. Separating the namespace leaves the same grant
  in place but points it at a namespace that holds only per-run Secrets; the
  listener's access to its *own* namespace is already narrowed by
  `resourceNames` to just its credentials Secret.
- **With NetworkPolicies off, runner code can reach the datastores directly.**
  Nothing stops a plan from opening a connection to Postgres (5432), Redis
  (6379), or the API (8000) without going through the BFF. Turning policies on
  denies all three; the runner policy permits only the API on 8000, HTTPS on
  443, and DNS.

Taken together: with both left at their defaults, treat "can queue a plan" as
close to "can read the control plane's Secrets".

**What the chart does for you once you choose it.** Every namespaced object the
separated topology needs is already rendered into the runner namespace — the
listener's `Role` and `RoleBinding` for Jobs, Pods, pod logs and Secrets, the
runner `ServiceAccount` (including its cloud workload-identity annotations), and
the runner `NetworkPolicy`. Two things are *about* the boundary and are handled
for you:

- The in-cluster API URL the runner receives is a FQDN
  (`<release>-api.<release-namespace>.svc.cluster.local:8000`), not a bare
  Service name, which would resolve only from the release namespace.
- The API's NetworkPolicy admits runners with a `namespaceSelector` scoped to the
  runner namespace. A bare `podSelector` matches only pods in the policy's own
  namespace, so without it the policy would deny every run's API call.

**What you must do yourself.** If `namespace.createRunner` is left false —
because your RBAC does not allow cluster-scoped writes, or you manage namespaces
outside Helm — create the namespace before installing, with the PSS labels above:

```sh
kubectl create namespace terrapod-runners
kubectl label namespace terrapod-runners \
  pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/audit=restricted \
  pod-security.kubernetes.io/warn=restricted
```

Otherwise `helm install` fails on the first object the chart places there, with
a message about a missing namespace rather than about the setting that asked for
it. A `ResourceQuota` on that namespace is also worth setting — see
[Deployment: sizing runner concurrency](deployment.md#sizing-runner-concurrency-on-a-fixed-resource-cluster).

Note that a chart-created namespace is **deleted by `helm uninstall`**, taking
any in-flight runner Job with it.

**Cross-cluster is stronger still.** A listener can run in a different cluster
entirely — the join flow is identical and the SSE connection is outbound — in
which case runner Jobs never share an API server with the control plane. See
[Architecture: agent pools](architecture.md).

### Other runner hardening


- **Short-lived runner tokens**: Each runner Job receives an HMAC-signed token scoped to its specific `run_id`, to that Job's **phase**, and to the time its run is **live** — with a configurable TTL (default 1h, max 2h) as the outer bound. The token is stored in a K8s Secret with `ownerReference` to the Job — automatically garbage-collected when the Job is cleaned up. The raw token never appears in the Job spec (injected via `secretKeyRef`)
- **Principle of least privilege**: Runner tokens carry only the `everyone` role. They can access binary cache downloads, provider mirror, and artifact endpoints for their own run — nothing else. Admin, write, and CRUD endpoints are inaccessible. Since 2.0 (GHSA-xmrf-hxq9-m59m) a plan-phase token also cannot reach the apply-phase endpoints (write state, post an apply result, upload an apply log) or vice versa, and the run's tokens stop working the moment it reaches a terminal state rather than at the end of their TTL. See [Runners → Per-phase auth Secret](runners.md#per-phase-auth-secret)
- **Authenticated API access**: All runner-facing endpoints (binary cache, provider mirror, artifact upload/download) require authentication. There are no unauthenticated endpoints that serve cached binaries or provider packages
- **Read-only root filesystem**: Enabled by default. Writable directories (`/workspace`, `/tmp`) use emptyDir volumes
- **No service account token**: `automountServiceAccountToken: false` by default (unless CSP identity is needed)
- **Non-root execution**: Runs as UID 1000
- **Dropped capabilities**: All Linux capabilities dropped
- **Seccomp profile**: RuntimeDefault
- **Resource limits**: CPU and memory limits prevent noisy-neighbor issues
- **Network isolation**: NetworkPolicies deny access to Postgres and Redis

### Plans on pull requests from forks

The isolation above bounds what a run can reach; it does not decide **whose**
code gets to run. A speculative plan executes the configuration on a pull
request branch with the workspace's own credentials — `env`-category
variables, sensitive values, OpenBao/Vault-resolved values, minted git
credentials and the Job's cloud workload identity.

For a pull request raised inside the repository that is the intended
behaviour: its author already has write access and can get code applied by
merging. A **fork** author has neither, so the speculative plan is the only
path by which their code reaches those credentials. Terrapod therefore does
not plan fork pull requests unless the workspace opts in:

```json
{ "data": { "type": "workspaces",
            "attributes": { "allow-fork-pr-plans": false } } }
```

`false` is the default from this release, so a new workspace is closed without
anyone doing anything. What a hardened deployment still has to check is the
workspaces that already exist: a **1.8 deployment defaulted it true**, and the
upgrade does not rewrite stored rows, so every workspace created before the
upgrade keeps whatever it had. Audit rather than assume:

```sql
SELECT name FROM workspaces WHERE allow_fork_pr_plans = true;
```

**To close them in one call** rather than one at a time — which is what the
migration note points here for:

```sh
curl -X POST "$TERRAPOD_URL/api/terrapod/v1/workspaces/actions/bulk-update" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"filter":{"all":true},
       "update":{"allow-fork-pr-plans":false},
       "dry_run":true}'
```

Note the shape: `filter`, `update` and `dry_run` sit at the **top level of the
body**, not inside a `data`/`attributes` envelope, and `dry_run` is spelled with
an underscore while the keys inside `update` are the kebab-case workspace
attribute names. This endpoint is admin-only.

`dry_run` defaults to true and reports what would change without changing it —
the identical code path, rolled back — so the preview is exactly what an apply
would do; send `"dry_run":false` to apply. The whole update is a single
transaction, all or nothing, and it never queues a run: the change lands on each
workspace's next normal run. `{"all":true}` has to be asked for explicitly; an
empty `filter` is a 422 rather than an implicit match-all. Narrow `filter` if you
want to keep a specific workspace open rather than re-opening it afterwards.

Every workspace in the audit result accepts code from people outside the
repository's write boundary. Keep the list to workspaces that hold nothing
worth taking — a public module repository taking community contributions is
the case it exists for — and check the autodiscovery rules too, since a rule
that sets it hands it to every workspace it creates from now on. See
[vcs-integration.md → Pull requests from
forks](vcs-integration.md#pull-requests-from-forks).

### Scope every VCS connection to an owner and a repository set

A VCS connection holds a GitHub App installation or a GitLab access token and
reaches **every repository that credential can reach**. Its id is returned to
anyone with `read` on a workspace using it, so the id is not a secret. Naming one
is therefore a grant, and Terrapod authorizes it: a caller must be a platform
admin, the connection's `owner-email`, reached by a role matching its `labels`, or
already own a workspace on it. Anything else is a **403**. (GHSA-v8g7-pqrj-8mcm)

That gate is on by default and needs no configuration. What a hardened deployment
should go on to do is the **two things it cannot decide for you**:

**1. Give each connection an owner or labels.** Without either, the only non-admin
claim left is "already owns a workspace on it", which means an admin has to create
each team's first workspace. Delegate instead:

```zsh
curl -X PATCH "$TERRAPOD/api/terrapod/v1/vcs-connections/vcs-<id>" \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{"data": {"type": "vcs-connections", "attributes": {
        "owner-email": "platform-lead@example.com",
        "labels": {"team": "platform"}}}}'
```

> **An `access` key is ignored on a connection** — deliberately, and unlike every
> other labelled resource. Honouring `access: everyone` would make the connection,
> and every repository its credential can reach, nameable by every authenticated
> user. So it is stripped before the labels are evaluated: harmless, but it will
> not delegate anything either. Use a role's `allow-labels` against the
> connection's other labels, or set `owner-email`.

**2. Narrow `allowed-repositories`.** This is **empty by default, and empty means
any repository the credential can reach**, so an upgrade changes nothing until you
set it. It is the control that stops an *entitled* caller pointing an entitled
connection at an unrelated repository in the same organization:

```zsh
curl -X PATCH "$TERRAPOD/api/terrapod/v1/vcs-connections/vcs-<id>" \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{"data": {"type": "vcs-connections", "attributes": {
        "allowed-repositories": ["platform-team/*"]}}}'
```

Audit which connections are still wide open, and who may name each:

```sql
SELECT name,
       provider,
       owner_email,
       labels,
       allowed_repositories
FROM   vcs_connections
WHERE  status = 'active'
ORDER  BY (allowed_repositories = '[]'::jsonb) DESC, name;
```

Rows at the top accept any repository the credential can reach. A row with an
empty `owner_email` **and** empty `labels` is one only an admin (or an existing
workspace owner) can build on.

**The allowlist is enforced at the clone, so there is no path that escapes it.**
It is checked at every path that *accepts* a repository URL — workspace create and
update, the refs endpoint, the config fetch, registry-module create, update and
VCS-update, and every minted git credential — and again inside the two functions
that actually use the credential, so a URL stored before a narrowing stops being
cloned rather than carrying on.

That second layer is not belt and braces. Checking only the accepting paths left
two holes. A URL set while a connection was wide kept being cloned afterwards, on
every path. And the VCS poller clones to detect a change *before* anything a run
would check, so a narrowed connection's credential had already read the
out-of-scope repository by the time the config fetch refused the run — which is the
thing the allowlist exists to prevent. Drift detection reached the archive cache the
same way. Earlier releases described the residual gap as the registry pollers'
clones alone; it was wider than that, and it is now closed.

Two consequences worth planning for. A narrowing takes effect on the **next clone**,
not at the moment you save it, so an in-flight run finishes against the old scope. And
because the refusal happens where the credential is used, it surfaces in a poll cycle
or a drift check — somewhere with no caller to receive a 403 — as a logged refusal
rather than an HTTP error. `docs/runbooks.md` has the symptoms.

**Scope the credential itself as well** for a bound that does not depend on Terrapod
at all: install the GitHub App on only the repositories it needs, or use a project- or
group-scoped GitLab token rather than one covering the whole instance. The allowlist is
then defence in depth over an already-narrow credential, which is where it is worth
the most.

Before narrowing a connection, list the
workspaces that would fall outside the new patterns — they keep their
configuration and start failing their next run:

```sql
SELECT w.name, w.vcs_repo_url
FROM   workspaces w
JOIN   vcs_connections c ON c.id = w.vcs_connection_id
WHERE  c.name = '<connection-name>'
ORDER  BY w.vcs_repo_url;
```

Refusals are visible in the audit log. It has no `status-code` filter, so select
on it client-side:

```zsh
curl -sH "Authorization: Bearer $TERRAPOD_TOKEN" \
  "$TERRAPOD/api/terrapod/v1/admin/audit-log?filter[resource-type]=workspaces&page[size]=100" \
  | jq '[.data[] | select(.attributes["status-code"] == 403)]'
```

Run-time refusals (a `git_http_auth` credential naming a connection the workspace
may not use) fail the run with the reason rather than running without the
credential, so they surface on the run itself, not as a 403.

Full semantics, including every point the allowlist is enforced at, are in
[vcs-integration.md → Naming a VCS
connection](vcs-integration.md#naming-a-vcs-connection-is-authorized).

### Do not treat workspace labels as a variable-set trust boundary

A variable set with an **assignment rule** selects workspaces by labels, name,
execution mode, agent pool, engine version, VCS connection and similar — and all
of those are settable by whoever has `admin` on the workspace. Workspace creation
is open and the creator becomes owner, so a rule describes a fleet; it is **not**
an authorization check, and a variable set has no per-set permissions for one to
appeal to. (GHSA-49q6-pm68-3xgw)

Terrapod enforces the one invariant available without an entitlement to consult:
a caller who is **not** a platform admin may not make a workspace **match** a
rule-assigned variable set it does not already match. Shrinking is allowed, as is
any edit to a workspace that already matches, and global or explicitly-assigned
sets never count. It is a **403** on workspace create and workspace `PATCH`.

This is a **behaviour change**: a non-admin who previously created workspaces that
joined a rule-scoped set now needs an admin to make the change, or an explicit
assignment. Expect it on first upgrade from self-service workflows and from a
non-admin running the OpenTofu/Terraform provider — see
[the runbook](runbooks.md#a-workspace-write-is-refused-with-a-403-about-a-variable-set).

For a hardened deployment the gate is a backstop, not the design. Audit the rules
whose sets carry credentials:

```sql
SELECT vs.name,
       vs.assignment_rule,
       count(vsv.id) FILTER (WHERE vsv.sensitive)       AS sensitive_vars,
       count(vsv.id) FILTER (WHERE vsv.value_source = 'vault') AS vault_vars
FROM   variable_sets vs
LEFT   JOIN variable_set_variables vsv ON vsv.variable_set_id = vs.id
WHERE  vs.assignment_rule IS NOT NULL
GROUP  BY vs.id, vs.name, vs.assignment_rule
HAVING count(vsv.id) FILTER (WHERE vsv.sensitive OR vsv.value_source = 'vault') > 0
ORDER  BY vs.name;
```

Any set in that result delivers a secret to **every workspace that can be made to
match**, which is a larger set than the one matching today. Assign those
explicitly, or move the value into the owning workspaces' own variables. Ask a set
who it currently reaches with
`GET /api/terrapod/v1/varsets/{id}/relationships/workspaces`, which reports
`explicit`, `global` or `rule` per workspace.

Refused workspace writes appear in the audit log the same way as above, on
`filter[resource-type]=workspaces` with `status-code` 403; the API server also
logs each one as `refused a workspace change that would pull in a rule-assigned
variable set`, with the actor and the variable-set names.

### Runner Token TTL

Tune token lifetimes based on your typical run duration:

```yaml
runners:
  tokenTTLSeconds: 3600       # Default token lifetime (1 hour)
  maxTokenTTLSeconds: 7200    # Hard ceiling — API rejects requests above this
```

For environments with fast runs, reduce the TTL to minimize the window of token validity. For long-running applies (large infrastructure), increase as needed.

For additional isolation, use:

```yaml
runners:
  nodeSelector:
    node-role.kubernetes.io/runner: "true"
  tolerations:
    - key: "runner"
      operator: "Exists"
      effect: "NoSchedule"
```

This schedules runner Jobs on dedicated nodes, isolating them from the control plane.

## Object Storage

- **Encryption at rest**: Enable SSE-S3/SSE-KMS (AWS), Azure Storage encryption, or GCS default encryption
- **Access logging**: Enable S3 access logging, Azure Storage analytics, or GCS audit logging
- **Bucket policy**: Restrict access to the Terrapod service account only
- **Versioning**: Enable object versioning for state file recovery

## Backup Strategy

- **PostgreSQL**: Daily automated backups with point-in-time recovery (PITR). Managed databases (RDS, Cloud SQL) handle this automatically
- **Object storage**: Enable versioning. Cross-region replication for disaster recovery
- **Redis**: Ephemeral by design (sessions, cache). No backup needed — data is reconstructed on restart
- **Secrets**: Back up Kubernetes Secrets to your secrets manager. They are the most critical non-reconstructable data
