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

## Per-workspace cloud identity

Two limitations of the OIDC federation added in v1.10.0. Full detail in
[Cloud Identity](cloud-identity.md).

- **No air-gapped variant, by design**. Making the credential boundary the
  workspace means a cloud has to fetch two documents
  (`/.well-known/openid-configuration` and `/.well-known/jwks.json`) from
  Terrapod, anonymously, before it will trust a token. The clouds publish no
  way to supply those out of band, so a deployment with no inbound path from
  the internet cannot use this feature — it is **mutually exclusive with the
  fully isolated recipe** in
  [Network Isolation](deployment-network-isolation.md). This is the trade the
  feature exists to make, not a gap we expect to close; an isolated deployment
  keeps the agent pool's own workload identity, which is what it had before.
  See [Cloud Identity → The trade](cloud-identity.md#the-trade-network-isolation-for-fine-grained-authorization).
- **A runner image predating the feature falls through silently** — *not yet*.
  Such a runner never asks for a token, so a workspace that maps targets runs
  as the agent pool's identity anyway. **Terrapod cannot detect it:** no
  runner-image version is reported to the API, so there is no server-side check
  to add, which is why this is listed as a limitation rather than fixed. It
  fails safe in one sense (the run authenticates as the pool, exactly as before)
  and unsafe in another (a workspace you deliberately moved off the pool's broad
  permissions is still using them). Upgrade runner images when you adopt this,
  and treat a federated workspace on a stale pool as mis-scoped until you have.
  See [Cloud Identity → The honest limitation](cloud-identity.md#the-honest-limitation-a-runner-image-that-predates-this-feature).

## Separating the plan phase from the apply

Terrapod can give a run's `plan` and its `apply` different identities — an
[apply-phase ServiceAccount](cloud-credentials.md#apply-phase-serviceaccount) and
[apply-only variables](cloud-identity.md#apply-only-variables) — so unreviewed
pull-request code runs under something that cannot change infrastructure. Both
are off by default. What they do **not** do:

- **A plan is not sandboxed, and it still reads.** A plan executes the
  configuration's code — module sources, provider binaries, `data` sources, a
  state refresh — so the plan identity needs **read** across the infrastructure
  the workspace manages. Narrowing the apply does not make a plan harmless; it
  bounds what the plan can *change*, not what it can run or see. Runner isolation
  (read-only root, dropped capabilities, no datastore reach, a run-scoped token)
  is what bounds the rest, and it is not a sandbox either. See
  [Security Hardening → Give the plan phase a narrower identity than the
  apply](security-hardening.md#give-the-plan-phase-a-narrower-identity-than-the-apply).
- **Terrapod does not verify that the plan identity is read-only, and cannot.**
  The cloud role behind a ServiceAccount is the operator's; Terrapod only decides
  which account a Job runs as. Two accounts annotated onto two roles that can
  both write is accepted and buys nothing, with no warning.
- **A plan still receives every variable not marked `apply-only`.** The flag is
  opt-in per variable, so `env` values, sensitive values, OpenBao/Vault-resolved
  values and minted git credentials reach a plan until you mark them — and the
  git-module credentials usually must, because `init` needs them.
- **There is no approval gate in front of a speculative plan.** A pull-request
  plan is dispatched when the pull request is pushed. The control over *whether*
  unreviewed code runs is
  [`allow-fork-pr-plans`](vcs-integration.md#pull-requests-from-forks), not this.
- **A listener image predating the apply-phase ServiceAccount ignores it.** The
  Job spec is built by the listener, and its config model ignores unknown keys,
  so an older listener runs every phase on `runners.serviceAccount` and says
  nothing. An ordinary `helm upgrade` moves both together; a deployment pinning
  `listener.image.tag` to an older release silently keeps one account. This is
  the same shape as the runner-image limitation above, and equally undetectable
  server-side.
- **The MCP server cannot declare the two-row pair.** `terrapod_variable_set` can
  set the flag, but it is an upsert keyed on `key` alone, so it cannot hold the
  same key twice — once ordinary and once apply-only. Use the API, the Terraform
  provider or the UI for a pair. See [MCP](mcp.md#tools).

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
