# Known Limitations

This page states plainly what Terrapod does **not** do — the constraints worth
knowing before you adopt it. Some are deliberate design boundaries; some are
"not yet" and are on the roadmap. We keep this list honest on purpose: it's
better to find a constraint here than in production.

Two categories are marked throughout:

- **By design** — a deliberate choice; not planned to change.
- **Not yet** — a current gap with an intended direction (issue linked where one exists).

## Deployment

- **Kubernetes only** *(by design)*. Terrapod deploys exclusively via its Helm
  chart, onto a Kubernetes cluster (a single-node [k3s](https://k3s.io/) VM
  counts). There is no Docker Compose, bare-metal installer, or Nomad target —
  the runner relies on the Kubernetes Jobs API, and the whole architecture
  assumes Kubernetes primitives. If you can't run Kubernetes, Terrapod isn't a
  fit. See [Getting Started](getting-started.md).
- **External PostgreSQL + Redis for production** *(by design)*. The production
  chart does not bundle datastores; you bring PostgreSQL 14+ and Redis 7+ (a
  managed service, or run them yourself). The chart *can* deploy in-cluster
  Postgres/Redis, but only for the **evaluation / dev** profiles
  (`make eval`, Tilt) — single-replica, no HA, no backups. Don't run those in
  production. See [Deployment](deployment.md).

## Organization, tenancy & access model

- **Single organization** *(by design)*. There is exactly one implicit
  organization, always named `default`, with no `org_name` anywhere in the data
  model or API. Multi-org is a SaaS multi-tenancy mechanism; a self-hosted
  install *is* one tenant. When you genuinely need two isolated tenants, run a
  second Terrapod instance (a second Helm release) — which gives stronger
  isolation than org-scoping inside one database. See
  [Architecture → Why a single organization](architecture.md#why-a-single-organization).
- **No teams; no projects** *(by design)*. Terrapod replaces TFE's team model
  with **label-based RBAC** — a "team" is a label, and access is resolved from
  labels + roles. There is no project concept. If your mental model depends on
  teams-as-objects or projects, you map those onto labels.

## Execution

- **`local` and `agent` execution modes only** *(by design)*. TFE's `remote`
  mode (TFE-hosted workers) is not supported and won't be — Terrapod has no
  built-in execution infrastructure; all server-side execution goes through
  agent pools. The API rejects `execution-mode: "remote"` with a 422.

## VCS integration

- **GitHub and GitLab only** *(not yet for others)*. VCS connections support
  GitHub (App) and GitLab (access token). Bitbucket and Azure DevOps are not
  supported; workspaces backed by them migrate as CLI-driven (no VCS
  connection). See [VCS Integration](vcs-integration.md).

## Migration

- **`terrapod-migrate` auto-creates a core subset** *(partly not yet)*. The tool
  imports VCS connections, workspaces, workspace variables, and state today.
  Variable sets, run triggers, notifications, agent pools, and the private
  registry are **read and reported** (surfaced as a checklist in the handover
  doc) but not yet auto-created — you recreate them by hand, typically via the
  Terraform provider. Extending this is tracked in
  [#709](https://github.com/mattrobinsonsre/terrapod/issues/709). RBAC is
  intentionally *suggested, never auto-applied*. See
  [Migration](migration.md#what-actually-transfers-today).

## Terragrunt

- **Agent-mode Terragrunt has caveats** *(by design + not yet)*. Terrapod runs
  Terragrunt in agent mode via the `terragrunt_enabled` workspace flag, and
  CLI-driven Terragrunt works with zero extra config. The migration tool does
  **not** auto-translate Terragrunt-driven Atlantis projects (their dependency
  graphs and `generate` blocks aren't mechanically convertible). See
  [Terragrunt](terragrunt.md) for the current boundaries.

## Policy

- **OPA/Rego only** *(by design)*. Policy-as-code uses Open Policy Agent and the
  Rego language — the open-source equivalent of TFE's Sentinel. Sentinel itself
  is proprietary and not supported; migrated Sentinel policies are listed by
  name for you to rewrite as Rego. See [Policy-as-Code](policies.md).

## Object storage

- **Native SDKs + filesystem; no S3-compat shim** *(by design)*. State and
  artifacts go to AWS S3, Azure Blob, or GCS via each provider's native SDK, or
  to a filesystem PVC for dev. There is no generic S3-compatible shim (and no
  bundled MinIO). See [Deployment](deployment.md).

## Explicitly out of scope (by design)

Terrapod orchestrates `terraform`/`tofu`; it does not reimplement them, and it
deliberately does not attempt:

- The Terraform/OpenTofu **engine** itself.
- **Sentinel** (proprietary policy language) and **Terraform Stacks**
  (proprietary orchestration runtime with no local-execution path).
- **Terraform Cloud Business-tier** SaaS features.
- A **built-in Vault** — configure Vault externally if you need it.
- **Non-Kubernetes** deployment of any kind.

---

Found something missing or inaccurate here? Please
[open an issue](https://github.com/mattrobinsonsre/terrapod/issues) — an honest
limitations list is only useful if it stays current.
