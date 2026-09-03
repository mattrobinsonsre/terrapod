# Known Limitations

This page states plainly what Terrapod does **not** do — the constraints worth
knowing before you adopt it. They are deliberate boundaries, and each one says
why. It's better to find a constraint here than in production.

## Deployment

- **Kubernetes only**. Terrapod deploys exclusively via its Helm
  chart, onto a Kubernetes cluster (a single-node [k3s](https://k3s.io/) VM
  counts). There is no Docker Compose, bare-metal installer, or Nomad target —
  the runner relies on the Kubernetes Jobs API, and the whole architecture
  assumes Kubernetes primitives. If you can't run Kubernetes, Terrapod isn't a
  fit. See [Getting Started](getting-started.md).
- **External PostgreSQL + Redis for production**. The production
  chart does not bundle datastores; you bring PostgreSQL 14+ and Redis 7+ (a
  managed service, or run them yourself). The chart *can* deploy in-cluster
  Postgres/Redis, but only for the **evaluation / dev** profiles
  (`make eval`, Tilt) — single-replica, no HA, no backups. Don't run those in
  production. See [Deployment](deployment.md).

## Organization, tenancy & access model

- **Single organization**. There is exactly one implicit
  organization, always named `default`, with no `org_name` anywhere in the data
  model or API. Multi-org is a SaaS multi-tenancy mechanism; a self-hosted
  install *is* one tenant. When you genuinely need two isolated tenants, run a
  second Terrapod instance (a second Helm release) — which gives stronger
  isolation than org-scoping inside one database. See
  [Architecture → Why a single organization](architecture.md#why-a-single-organization).
- **No teams; no projects**. Terrapod replaces TFE's team model
  with **label-based RBAC** — a "team" is a label, and access is resolved from
  labels + roles. There is no project concept. If your mental model depends on
  teams-as-objects or projects, you map those onto labels.

## Execution

- **`local` and `agent` execution modes only**. TFE's `remote`
  mode (TFE-hosted workers) is not supported and won't be — Terrapod has no
  built-in execution infrastructure; all server-side execution goes through
  agent pools. The API rejects `execution-mode: "remote"` with a 422.

## VCS integration

- **GitHub and GitLab**. VCS connections support GitHub (App) and GitLab
  (access token). Workspaces backed by other providers run CLI-driven, without a
  VCS connection — the full run lifecycle, state and policy still apply; the
  push/PR triggers are what a connection adds. See
  [VCS Integration](vcs-integration.md).

## Migration

- **`terrapod-migrate` auto-creates a core subset**. The
  tool imports VCS connections, workspaces, workspace variables, state, variable
  sets, run triggers, notification configurations, agent pools, and registry GPG
  signing public keys today (agent pools migrate the pool identity + re-point
  member workspaces; tokens are never portable, so each pool is reported as
  needing a fresh join token + redeployed listeners). Registry **module and
  provider versions** are **read and reported**, not auto-created — this is a
  limit of the source API and the signing model, not missing work: the source
  API doesn't return published module tarballs (re-publish, or point Terrapod at
  the module's VCS tag stream), and a provider version's detached signature
  needs the operator's **private** signing key to re-create (re-publish with
  `terrapod-publish`; the public key is migrated for you). RBAC is intentionally
  *suggested, never auto-applied* — an import that silently granted access
  would be the one thing you could not review afterwards. See
  [Migration](migration.md#what-actually-transfers-today).

## Terragrunt

- **Agent-mode Terragrunt has caveats**. Terrapod runs
  Terragrunt in agent mode via the `terragrunt_enabled` workspace flag, and
  CLI-driven Terragrunt works with zero extra config. The migration tool does
  **not** auto-translate Terragrunt-driven Atlantis projects (their dependency
  graphs and `generate` blocks aren't mechanically convertible). See
  [Terragrunt](terragrunt.md) for the current boundaries.

## Policy

- **OPA/Rego only**. Policy-as-code uses Open Policy Agent and the
  Rego language — the open-source equivalent of TFE's Sentinel. Sentinel itself
  is proprietary and not supported; migrated Sentinel policies are listed by
  name for you to rewrite as Rego. See [Policy-as-Code](policies.md).

## Object storage

- **Native SDKs + filesystem; no S3-compat shim**. State and
  artifacts go to AWS S3, Azure Blob, or GCS via each provider's native SDK, or
  to a filesystem PVC for dev. There is no generic S3-compatible shim (and no
  bundled MinIO). See [Deployment](deployment.md).

## Explicitly out of scope (by design)

Terrapod orchestrates `terraform`/`tofu`; it does not reimplement them, and it
deliberately does not attempt:

- The Terraform/OpenTofu **engine** itself.
- **Sentinel** (proprietary policy language) and **Terraform Stacks**
  (proprietary orchestration runtime with no local-execution path).
- **Terraform Cloud Business-tier**. SaaS features.
- **Non-Kubernetes** deployment of any kind.

---

Found something missing or inaccurate here? Please
[open an issue](https://github.com/mattrobinsonsre/terrapod/issues) — an honest
limitations list is only useful if it stays current.
