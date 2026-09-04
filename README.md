# Terrapod

[![CI](https://github.com/mattrobinsonsre/terrapod/actions/workflows/ci.yml/badge.svg)](https://github.com/mattrobinsonsre/terrapod/actions/workflows/ci.yml)
[![License: MPL 2.0](https://img.shields.io/badge/License-MPL%202.0-brightgreen.svg)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/mattrobinsonsre/terrapod?sort=semver&label=release)](https://github.com/mattrobinsonsre/terrapod/releases/latest)

**A free, open-source, self-hosted alternative to Terraform Enterprise and Terraform Cloud (HCP Terraform)** — a **TACOS** (Terraform Automation and Collaboration Software) platform for teams standardizing on `terraform`, OpenTofu (`tofu`), or Terragrunt. Not a fork of either engine; it orchestrates them.

- **Point your existing `cloud` block at it.** Terrapod implements the slice of the [TFE V2 API](https://developer.hashicorp.com/terraform/enterprise/api-docs) that `terraform` and `tofu` actually consume — the `cloud`/`remote` backend protocol, as the CLI speaks it via [`go-tfe`](https://pkg.go.dev/github.com/hashicorp/go-tfe) — so CLI runs and CI/CD usually move across with zero code changes. That slice is the compatibility target: everything beyond it is Terrapod's own API rather than a reimplementation of the full TFE V2 surface.
- **Your state and secrets never leave your boundary.** Versioned remote state in your Postgres and object store, cloud credentials via Kubernetes workload identity — nothing long-lived, nothing vendor-held.
- **Governance in the box.** Label-based RBAC, OPA/Rego policy-as-code (the open-source equivalent of TFE's Sentinel), a private module and provider registry, cost estimation, security scanning, and an AI review layer.
- **A low bar to run.** Your own Kubernetes — and that means a single-node [k3s](https://k3s.io/) VM is plenty to start, not a managed cluster or Kubernetes expertise.
- **Free, and staying free.** **MPL-2.0** (the same license as OpenTofu and the historical Terraform codebase); self-hosted internal use triggers no source-disclosure obligation. No commercial edition, no open-core split, no paid tier, no per-resource pricing, now or planned — the complete platform is in this repository.

Comparing options? Start with **[Alternatives to Terraform Enterprise / Terraform Cloud](docs/alternatives.md)** and the **[FAQ](docs/faq.md)**.

![A Terrapod run overview: six cards showing changes, monthly cost, AI risk, policy result, security scan and runner resources](docs/images/run-detail.png)
*Everything a reviewer needs before approving a change, on one screen.*

**Which path are you on?** — [Evaluate](#quick-evaluation) (`make eval`, one command) · [Deploy](#quick-start) (Helm on your cluster) · [Migrate](docs/migration.md) (off TFE / HCP / Atlantis, reversible) · [Contribute](CONTRIBUTING.md)

---

## Quick Evaluation

Try Terrapod end-to-end on your laptop in one command. It spins up a throwaway
[kind](https://kind.sigs.k8s.io/) or [k3d](https://k3d.io/) cluster and installs
a complete, self-contained stack — in-cluster PostgreSQL + Redis, filesystem
storage, a local admin login — with no cloud account and no external
dependencies. It even seeds a sample workspace + a completed plan, so you land
on a populated UI, not an empty list:

![Signing in, the workspace list, and a completed run showing its changes, monthly cost, AI risk verdict, policy result and security scan](docs/images/eval-demo.gif)

```sh
make eval          # create a local cluster + install Terrapod, then port-forward
# → open http://localhost:8080  (login: admin / terrapod)

make eval-down     # delete the whole thing when you're done
```

Prerequisites: Docker, `kubectl`, `helm`, and either `kind` or `k3d`. The
quickstart pulls released images, so the only wait is the image download.

> This is an **evaluation** profile — single-replica in-cluster datastores, a
> known password, no HA or backups. For a real deployment see
> [docs/deployment.md](docs/deployment.md); for the design behind the K8s-only
> stance and how to enable agent execution, see [docs/getting-started.md](docs/getting-started.md).

---

## Coming from Terraform Enterprise / HCP Terraform?

CLI-driven runs and CI/CD point at Terrapod unchanged. Where the *models* differ (structural facts, not a feature-by-feature scorecard):

| | HCP Terraform / TFE | Terrapod |
|---|---|---|
| Hosting | Vendor SaaS, or a self-managed distribution | Self-hosted on your own Kubernetes |
| Licensing & cost | Proprietary (BUSL), priced by managed resources | Free and open source (MPL-2.0) |
| Where state + secrets live | On the vendor / self-managed control plane | Never leave your boundary (your Postgres + object store) |
| Cloud credentials | Vendor-stored or dynamic | K8s workload identity (IRSA / WIF / Azure WI) — nothing long-lived |
| Policy engine | Sentinel (proprietary) | OPA / Rego (open) — advisory or mandatory |
| Restricted-network / air-gap execution | SaaS-dependent by default | First-class — outbound-only runners, polling VCS, pull-through mirror + sealed cache-only mode |
| Private registry · RBAC · SSO · Audit | Yes | Yes — self-hosted equivalents (label RBAC; OIDC/SAML; immutable audit) |
| Multi-organization | Yes | Single org by design ([run an instance per tenant](docs/architecture.md#why-a-single-organization)) |
| CLI `cloud`-backend API | TFE V2 | The CLI-consumed subset of TFE V2 (rest is Terrapod's own API) |

*Confirm current HCP terms with HashiCorp.*

> **Ready to move?** [`terrapod-migrate`](docs/migration.md) does it dry-run-first and fully reversible — preview every workspace, variable, variable set, VCS connection, state file (serial + lineage preserved), run trigger, notification, agent pool, and registry signing key it will create, apply, verify parity, then roll back cleanly if needed. Registry module/provider *versions* are reported for re-publish rather than auto-created (a source-API + signing-key limitation, [documented plainly](docs/migration.md#what-actually-transfers-today)).

---

## What you may not expect

The platform tier is all here — workspaces, versioned state with locking and
rollback, the plan → apply lifecycle, a GPG-signed private registry, variables
and variable sets, run triggers, notifications, drift detection, label-based
RBAC and OPA policy — and the [feature table](#features) lists it in full. These
are the parts worth knowing about before you read that far:

- **Runs anywhere your network is awkward.** Runners dial *out* and create Kubernetes Jobs locally, so the control plane never needs inbound reach into an execution cluster — isolated VPCs, other regions, on-prem, or behind egress-only firewalls. VCS is polled outbound, and a pull-through provider mirror + binary cache (with an air-gap sealed mode) lets runners resolve providers and binaries with no upstream internet for cached platforms. (The API itself still needs an outbound path to fill those caches — internal mirrors and a forward proxy both work; see [network isolation](docs/deployment-network-isolation.md).)
- **Zero static cloud credentials.** Runs and the platform reach cloud APIs through Kubernetes workload identity (AWS IRSA, GCP WIF, Azure WI) — nothing long-lived to store, leak, or rotate.
- **Cost visibility built in** — no third-party service. See the monthly cost of your managed infrastructure right in the run and workspace: the monthly *total* and the *change this run introduces* on every run, and the current total on each workspace, priced by a native engine over Terrapod's own self-generated pricesheet (AWS, Azure, GCP; air-gap-friendly). On by default; an optional AI layer estimates what the engine can't price and answers a grounded [cost chat](docs/cost-estimation.md).
- **A container registry in the box.** Terrapod serves an OCI registry at `/v2/`, so the images your runs need live where your state, modules and providers already do — same auth, same storage, same network boundary, no second system to deploy and keep alive. Push and pull with docker, skopeo or crane; a Kubernetes `imagePullSecret` works with an ordinary Terrapod token. It mirrors upstream registries pull-through behind an explicit allow-list, or runs push-only for an air-gapped install. Nothing is anonymous, reads included. [Container registry](docs/oci-registry.md).
- **Your dependencies, cached too.** Terrapod proxies PyPI and npm alongside providers, engine binaries and container images, so a run resolves its whole dependency closure without reaching the internet — point it at `PIP_INDEX_URL` and `NPM_CONFIG_REGISTRY` and seal the network. Upstream's integrity hashes are passed through untouched, so your client still verifies against the package author's digest. [Package proxies](docs/package-cache.md).
- **An AI-augmented review layer** — optional and off by default — plan change-summaries, risk assessment, failure analysis, and a chat to interrogate a run.

![An AI plan summary explaining why an ECS service must be replaced rather than updated, citing the immutable fields responsible](docs/images/run-ai-analysis.png)
*The AI review reads the plan, not a template: here it works out that `launch_type` and `scheduling_strategy` are immutable, which is why the service is replaced rather than updated, and what that means for availability.*

- **A reversible, dry-run-first migration** off TFE / HCP Terraform / Atlantis with [`terrapod-migrate`](docs/migration.md) — preview everything, apply, verify parity, and roll back cleanly.

**Only interested in Terraform and OpenTofu?** Then say so, and none of the rest is
deployed. The container registry and the PyPI and npm proxies are there to serve
Ansible and Pulumi work; two Helm switches —
`api.config.engines.ansible.enabled` and `api.config.engines.pulumi.enabled` —
turn each off completely. Their routes are never registered, their background
tasks never scheduled, and nothing appears in the UI or the API schema. Terraform
and OpenTofu are unaffected either way: the provider mirror, the engine binary
cache and the module registry are what Terrapod is, and are never gated. Turning
one off is not destructive — anything already stored stays put and comes back if
you turn it on again.

The one hard requirement is Kubernetes, and that's a low bar: Terrapod is a single Helm release, a one-node [k3s](https://k3s.io/) VM is plenty to start, and `make eval` spins up a throwaway [k3d](https://k3d.io/)/kind cluster in one command.

Three deliberate design foci set Terrapod apart, each with a doc to go deeper: **restricted-network & multi-cluster execution** (outbound-only runners + polling VCS + a self-contained provider mirror/binary cache with a sealed air-gap mode — see [network isolation](docs/deployment-network-isolation.md) and the [ARC execution model](docs/architecture.md#runner-architecture-arc-pattern)); an **AI-augmented review layer** (provider-agnostic via [LiteLLM](https://github.com/BerriAI/litellm), off by default — see [AI plan summary](docs/ai-plan-summary.md)); and a **low contribution barrier** (a Python platform core, AI-assisted contributions welcome — see [`llms.txt`](llms.txt) and [AGENTS.md](AGENTS.md)).

---

## Running it in production

The de-risking signals, in one place:

- **High availability across three planes — and honest about which need a human** — the **control plane** runs multi-replica with **no leader election** (any replica does any job, coordinated through Redis; PDBs on by default); the **execution plane** routes each workspace to a *set* of agent pools, so losing a pool, a cluster, or a region's execution capacity is absorbed automatically with no failover step to get wrong; the **data plane** is a warm leader/follower pair, and **failover is a deliberate human act — you move DNS. Terrapod never votes or arbitrates, so it cannot split-brain your state.** Pairs work **across regions and across clouds** (each node owns its own database and object store, and speaks its provider's native SDK — S3↔Azure Blob↔GCS↔on-prem), with per-prefix-class control over what is copied versus merely verified. See [HA topologies](docs/ha-topologies.md) for multi-region / multi-cloud / listener layouts, and [High availability](docs/high-availability.md) for the mechanism.
- **Horizontal scale to large workspace counts — measured, and reproducible** — the control plane scales out (stateless API replicas, run dispatch via `SELECT … FOR UPDATE SKIP LOCKED`); for platform admins and auditors, paged workspace-list reads are O(page) rather than O(estate) — a flat ~20 ms p50 from 2,000 to 10,000 workspaces, against 111 ms at 2,000 before; label-RBAC users keep the filter-then-paginate path. A committed load-test harness ([`loadtest/`](loadtest/)) and a horizontal-scale Helm profile ([`values-scale.yaml`](helm/terrapod/values-scale.yaml)) let you measure it yourself. See [Scalability](docs/scalability.md).
- **Enterprise identity & access** — SSO via OIDC and SAML (Auth0, Okta, Azure AD, …), plus `terraform login` (OAuth2 + PKCE) and long-lived API tokens for automation; granular label-based RBAC with `resource:verb` capabilities. See [Authentication](docs/authentication.md) · [RBAC](docs/rbac.md).
- **Immutable audit** — a tamper-evident, retention-configurable audit log of every API action. See [Audit logging](docs/audit-logging.md).
- **Hardened by default** — every pod runs non-root, read-only root filesystem, all capabilities dropped, and a seccomp profile. See [Security hardening](docs/security-hardening.md) · [Production checklist](docs/production-checklist.md).
- **Verifiable supply chain** — every release image and the Helm chart is keyless-signed with cosign and carries SBOM (SPDX) + SLSA build-provenance; verify with `cosign verify` / `gh attestation verify` before you deploy. See [Supply-chain verification](docs/supply-chain-verification.md).
- **A versioning contract, machine-enforced** — v1.x holds a SemVer promise per public surface (HTTP routes, response attributes, the runner wire protocol, config keys, Helm values, DB schema), each pinned by a committed snapshot that fails CI on a breaking change. See [Versioning & support](docs/versioning-and-support.md).
- **Backup & disaster recovery** — an optional shipped `pg_dump` backup CronJob, a restore-verification DR drill (a real green check, not a doc), and documented break-glass state recovery straight from object storage. See [Disaster recovery](docs/disaster-recovery.md).
- **Reversible upgrades & migration** — every schema migration ships a real `upgrade()`/`downgrade()` and the chart is the single upgrade unit, so version bumps are auditable and reversible; migrating *in* off TFE / HCP / Atlantis is dry-run-first and roll-back-able. See [Deployment](docs/deployment.md) · [Migration](docs/migration.md).

---

## Features

Everything below is implemented and shipped today.

### Core platform

| Feature | Description |
|---|---|
| Workspaces | Isolate state, variables, and runs per workspace |
| Remote state | Versioned state with locking and rollback; encrypted at rest by your object store, with optional app-layer BYOK envelope encryption |
| CLI-driven runs | `terraform` / `tofu` plan / apply via the `cloud` backend (both verified) |
| Terraform / OpenTofu provider | **Manage Terrapod itself as code** — [`terraform-provider-terrapod`](docs/terraform-provider.md) ships **25 resources + 9 data sources** (`terrapod_workspace`, `terrapod_variable`, `terrapod_role`, `terrapod_vcs_connection`, `terrapod_agent_pool`, `terrapod_run_task`, `terrapod_catalog_item`, `terrapod_execution_hook`, …), served per-instance from `<host>/default/terrapod` and GPG-signed |
| AI agent integration (MCP) | **Drive Terrapod from an AI agent** (Claude, Cursor, …) via an official [MCP server](docs/mcp.md), `terrapod-mcp` — a local stdio binary authed with your `tofu login` token. Read-rich Observe tools (workspaces, runs, **structured plan JSON**, drift) + gated Act tools (plan/apply through the normal RBAC'd lifecycle). One server per instance = strict prod/dev isolation |
| Agent execution | Server-side plan / apply on ephemeral K8s Jobs (ARC pattern) |
| Agent pools | Named runner-listener groups; join-token → certificate exchange for auth |
| TFE V2 CLI surface | The `cloud`-backend subset of the TFE V2 API (JSON:API) consumed by `terraform`/`tofu` + `terraform login` — not the full TFE V2 API |
| Run triggers | Cross-workspace dependency chains — a source apply triggers downstream runs |
| Conditional auto-apply | Auto-apply only when the plan is within a declared safety standard — adds only, or adds and in-place updates. Anything that destroys or replaces a resource stops for a human |
| Workspace undelete | Deleting a workspace leaves its state behind a delete marker, so an admin can salvage it into a new workspace within the retention window. A salvage operation, not an undo — the recovered workspace has a new id and comes back inert |
| Stale-plan guards | Auto-discard a plan that no longer reflects reality: state-version drift (always on) + optional per-workspace time-based plan expiry |

### Governance & security

| Feature | Description |
|---|---|
| Label-based RBAC | Roles with granular `resource:verb` capabilities (e.g. `run:plan` without `run:apply`); read/plan/write/admin levels remain as authoring shorthand |
| Policy-as-code (OPA) | Rego enforcement on plan JSON — the open-source equivalent of Sentinel. Advisory or mandatory sets, label-scoped to workspaces, evaluated on the runner, with admin override |
| IaC security scanning | Checkov/Trivy misconfiguration scanning of the plan JSON with maintained rule catalogues — per-workspace `off`/`advisory`/`enforced`, severity threshold, skip rules; enforced holds the run at the gate on a failed finding, with admin override |
| SSO (OIDC / SAML) | Pluggable identity providers (Auth0, Okta, Azure AD, any standards-compliant IdP) |
| Audit logging | Immutable event log with configurable retention |
| Cloud credentials | Zero static keys — dynamic credentials via K8s workload identity (AWS IRSA, GCP WIF, Azure WI); passwordless DB and Redis IAM auth |
| Supply-chain verification | Cached binaries + provider archives verified against the publisher's GPG-signed SHA256SUMS (pinned keys); the runner re-verifies the executable before running it |
| Signed releases | Every release image + the Helm chart is keyless-signed with cosign, with per-image SBOM (SPDX) + SLSA build-provenance attestations — verifiable with `cosign verify` / `gh attestation verify` |

<details>
<summary><strong>Registry &amp; caching · Integrations &amp; operations · AI</strong> (click to expand)</summary>

### Registry & caching

| Feature | Description |
|---|---|
| Private module registry | Publish, version, and share modules internally |
| Private provider registry | Publish, version, and share providers with GPG signing and network-mirror caching |
| Binary caching | Pull-through cache for the terraform / tofu / terragrunt CLI binaries |
| Cache pre-population | Bulk-warm the binary + provider caches ahead of time via an admin endpoint + UI panel (for restricted-network / fast-first-run deployments) |
| Sealed (cache-only) mode | Air-gap switch (`registry.cache_only`) guaranteeing no upstream fetch — flipped on *after* the caches are populated — cache-backed version resolution, actionable cache-miss errors, retention skips the caches |

### Integrations & operations

| Feature | Description |
|---|---|
| VCS integration | GitHub App + GitLab token; inbound webhooks supported (GitHub HMAC + GitLab token) for instant triggers, with outbound polling as the resilient default — so webhooks are optional, never required |
| Workspace autodiscovery | Atlantis-style monorepo autodiscovery — pattern-matched rules auto-create workspaces on PRs to new directories |
| Terragrunt | Per-workspace Terragrunt for agent-mode runs (a flag + pinned version, pull-through binary cache, local-backend reconciliation so Terrapod still owns state); CLI-driven runs need no extra config |
| Variables & secrets | Per-workspace env and Terraform variables; sensitive values protected by database encryption-at-rest; variable sets, assignable by rule (labels/globs) as well as one by one; values can be [read from HashiCorp Vault](docs/vault.md) at run time, including dynamic secrets |
| Private module source auth | First-class auth for private `git::https://` / `git::ssh://` module sources — a scoped `git_http_auth` / `git_ssh_auth` variable (static token or minted from a VCS connection), with ssh↔https protocol rewriting; credentials are log-safe and delivered only via the per-run Secret ([module-auth.md](docs/module-auth.md)) |
| Drift detection | Scheduled plan-only runs to detect out-of-band changes, with a per-workspace ignore allowlist |
| Notifications | Webhook (HMAC-SHA512), Slack (Block Kit), and email alerts on run events |
| Interactive Slack app | Outbound Socket Mode app — `/terrapod` account linking + opt-in per-workspace run notifications with RBAC-checked Approve/Discard buttons; multiple deployments can share one Slack workspace |
| Run tasks | Pre/post-plan webhook hooks for external validation |
| Execution hooks | **Custom execution steps** — admin-managed shell run in the runner Job at pre_init / pre_plan / post_plan / pre_apply / post_apply, associated with workspaces (`pre_init` is the setup/tooling/auth slot; custom runner images cover heavier needs) |
| Service catalog | No-code self-service provisioning over the module registry |
| Cost estimation | Monthly cost of managed infrastructure — a per-plan delta on every run and the current total on a workspace; data via a native cost engine, on by default; optional AI layer estimates the resources the engine can't price + savings advisories + a grounded cost chat |
| Impact graph | Interactive dependency + blast-radius view of a plan on the run page — module-clustered, click a resource to light up its transitive downstream impact |
| Estate topology | Whole-estate dependency graph — workspaces + modules wired by run-triggers, remote-state, and module links; group by any label / pool / name prefix; RBAC-filtered; accessible table fallback |
| State resource graph | Per-workspace resource dependency graph from Terraform state — resources wired by `depends-on`; current state version by default with an older-version picker; group by type / module / provider / mode; accessible table fallback |
| Workspace health | Per-workspace health conditions, VCS polling status, drift detection indicators |
| Internationalization | Web UI translated into **27 languages** (next-intl) — European, Asian and Middle Eastern — with the locale resolved per-request (`NEXT_LOCALE` cookie → `Accept-Language` → `en`), selectable on the login screen and via a nav switcher. AI plan summaries are translated at view time; two CI gates keep every offered language 100% complete (no partial locales) and block untranslated UI strings |

### Migration & onboarding

| Feature | Description |
|---|---|
| Migrate in from TFE / HCP / Atlantis | [`terrapod-migrate`](docs/migration.md) — a dry-run-first, reversible CLI that moves an existing Terraform Enterprise / HCP Terraform / Atlantis platform onto Terrapod: previews, then creates VCS connections, workspaces, variables, variable sets, state (serial + lineage preserved), run triggers, notifications, agent pools, and registry signing keys; verifies parity and rolls back cleanly. Registry module/provider *versions* are reported for re-publish (a source-API + signing-key limit), and RBAC is suggested, never auto-applied |
| Discover existing resources into IaC | [`terrapod-query`](docs/terrapod-query.md) — a **tofu-native** discovery engine that finds existing, unmanaged cloud resources and emits `import {}` blocks (schema → filtered `data`-source query → import blocks; MPL, no BUSL). It rides **data sources** rather than a provider's Terraform-1.14 `list`-resource support, so by design it doesn't require a provider to ship `list` resources — it discovers through the data sources providers already expose (`terraform query` takes the complementary route, driving a provider's Terraform-1.14 `list` resources). Usable **standalone by any OpenTofu (or Terraform) user**, and baked into the API + runner images to drive the in-product onboarding flow (import is always a gated, import-only run). Optional AI polish renames/groups/comments the generated config without ever touching a value or import id |

### AI (optional, off by default)

| Feature | Description |
|---|---|
| AI plan review | LLM change summary + risk assessment on every plan, failure analysis on errored plans, and a chat to interrogate a run — provider-agnostic via LiteLLM (AWS Bedrock, OpenAI, Anthropic, Gemini, Azure OpenAI, vLLM). IAM-native auth for Bedrock (IRSA + optional cross-account `sts:AssumeRole`). When security scanning and/or cost estimation are on, the same summary adds a **grounded design review** — extra `risk_factors` tagged with a category (security/reliability/cost/operations/scalability/change/other), grounded in the deterministic Checkov/Trivy findings and the cost estimate; **advisory only, never gates a run** |
| AI architecture critique | Optional AI review of a workspace's **deployed system as it exists** — inferred from its latest state and critiqued across reliability/security/cost/operations/scalability. Distinct from the per-run plan summary (which reviews a *change*). Grounded, never invented: security ← the Checkov/Trivy scan, cost ← the native cost engine, the rest ← state + the resource graph. Its own `ai_architecture` config (own model + budget, off by default); auto-generated on a new state version + on-demand regenerate |

</details>

### More screenshots

<details>
<summary>The plan output, the workspace list, and the impact graph</summary>

![Plan output showing an ECS service that must be replaced, with launch_type and scheduling_strategy marked "forces replacement"](docs/images/run-log.png)
*The same run's plan output — the reason a human is asked to approve it. `launch_type` and `scheduling_strategy` are immutable, so the service is destroyed and rebuilt rather than updated, and the log says so in as many words.*

![Workspace list table with name, mode, pool, resources, status and created columns](docs/images/workspaces.png)
*An estate: per-workspace execution mode, agent pool, runner sizing and live status, filterable by label.*

![A 3D force-directed graph of plan resources, coloured by action, with one node's dependents highlighted](docs/images/impact-graph.png)
*The impact graph — a plan clustered by module, with a resource's downstream blast radius highlighted.*
</details>

---

## Architecture

```mermaid
flowchart TB
    user["Browser / CLI"]

    subgraph control["Terrapod control plane"]
        bff["Next.js BFF<br/>web UI + proxy"]
        api["FastAPI API<br/>multi-replica, no leader election"]
    end

    subgraph stores["Your data — never leaves your boundary"]
        pg[("PostgreSQL<br/>state + metadata")]
        redis[("Redis<br/>sessions, locks, events")]
        obj[("Object storage<br/>state, plans, logs")]
    end

    subgraph exec["Execution cluster — any network"]
        listener["Runner listener"]
        job["Ephemeral K8s Job<br/>terraform / tofu"]
    end

    user -->|HTTPS| bff
    bff -->|the only ingress| api
    api --- pg
    api --- redis
    api --- obj
    listener -.->|"outbound SSE - the control plane never dials in"| api
    listener -->|creates| job
```

The execution cluster reaches the control plane, never the other way round — which is what lets runners sit in an isolated VPC, another region, or on-prem behind an egress-only firewall. Run lifecycle, the reconciler, storage layout and the runner protocol are in [docs/architecture.md](docs/architecture.md).

The full picture — run lifecycle, the reconciler, storage layout and the
runner protocol — is in [docs/architecture.md](docs/architecture.md).

### Design Principles

- **API-first** — every UI action is backed by a public API endpoint
- **BFF pattern** — the Next.js frontend is the single ingress entry point; the browser never talks to the API directly
- **Responsive, mobile-first web UI** — the whole UI adapts from desktop tables to touch-friendly card layouts on phones; one DRY viewport-driven implementation, no separate mobile app
- **Kubernetes-native** — deployed exclusively via the Helm chart; runner Jobs are ephemeral K8s Jobs
- **ARC-pattern execution** — the listener creates Jobs on demand (like GitHub Actions Runner Controller)
- **OpenTofu-first** — [OpenTofu](https://opentofu.org/) is the recommended execution backend; `terraform` is also supported
- **Single organization** — one org per instance (the literal name `default`), a deliberate self-hosted fit. Need separate tenants? Run an instance per tenant. See [Why a single organization](docs/architecture.md#why-a-single-organization)
- **Native object storage** — speaks each cloud provider's native SDK (S3, Azure Blob, GCS) with filesystem fallback for dev

---

## Quick Start

Terrapod runs **only on Kubernetes** (the runner uses the Jobs API). Deploy it onto any cluster — or a single-node [k3s](https://k3s.io/) VM — with the Helm chart.

### Prerequisites

- A Kubernetes cluster (1.29+). No cluster? `curl -sfL https://get.k3s.io | sh -` gives you one on a single VM, with an ingress controller (Traefik) and storage included.
- Helm 3.x
- **External** PostgreSQL 14+ and Redis 7+ (the chart does not bundle a production-grade datastore; it can deploy in-cluster Postgres/Redis via `postgresql.deploy`/`redis.deploy` for eval/dev only) — use a managed service or run them on the cluster/VM.

### Deploy

```zsh
helm install terrapod oci://ghcr.io/mattrobinsonsre/terrapod \
  --namespace terrapod --create-namespace \
  --set ingress.enabled=true \
  --set ingress.hostname="terrapod.example.com" \
  --set ingress.className=traefik \
  --set postgresql.url="postgresql+asyncpg://terrapod:PASSWORD@PGHOST:5432/terrapod" \
  --set redis.url="redis://REDISHOST:6379" \
  --set bootstrap.adminEmail="admin@example.com" \
  --set bootstrap.adminPassword="change-me-now"
```

Defaults give you filesystem storage on a PVC, local password auth, the migrations job, and a bootstrap admin user. Point your hostname's DNS at the ingress controller, then open `https://terrapod.example.com` and log in. (For a quick HTTP-only look, add `--set ingress.tls=false`.)

Object storage options: S3, Azure Blob, GCS, or the default PVC-backed filesystem.

### Verify what you're deploying (optional)

Every released image and the Helm chart are keyless-signed with [cosign](https://github.com/sigstore/cosign) and carry SBOM (SPDX) + SLSA build-provenance attestations. To verify an image's signature and provenance before you deploy:

```zsh
# Signature (keyless — identity pinned to the release workflow's GitHub OIDC):
cosign verify ghcr.io/mattrobinsonsre/terrapod-api:vX.Y.Z \
  --certificate-identity-regexp '^https://github.com/mattrobinsonsre/terrapod/\.github/workflows/ci\.yml@refs/tags/v.*$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

# SBOM + build provenance (discoverable as OCI referrers on the same digest):
gh attestation verify oci://ghcr.io/mattrobinsonsre/terrapod-api:vX.Y.Z --repo mattrobinsonsre/terrapod
```

Full details and admission-time enforcement patterns: [Supply-chain Verification](docs/supply-chain-verification.md#verifying-terrapods-own-release-artifacts).

### Create Your First Workspace

```zsh
# Create an API token in the UI (Settings → API Tokens), or: tofu login terrapod.example.com
export TERRAPOD_TOKEN="<your-api-token>"

curl -X POST https://terrapod.example.com/api/v2/organizations/default/workspaces \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "workspaces",
      "attributes": {
        "name": "my-first-workspace"
      }
    }
  }'
```

### Configure OpenTofu (or Terraform)

```hcl
# main.tf
terraform {
  cloud {
    hostname     = "terrapod.example.com"
    organization = "default"

    workspaces {
      name = "my-first-workspace"
    }
  }
}
```

```zsh
tofu login terrapod.example.com
tofu init
tofu plan
tofu apply
```

For the full walkthrough (k3s bootstrap, DNS/ingress, agent mode, variables, registry) see [docs/getting-started.md](docs/getting-started.md). For the complete production deployment guide — storage backends, external DB, SSO, scaling, TLS — see [docs/deployment.md](docs/deployment.md). To run Terrapod **from source** as a contributor, see [docs/local-development.md](docs/local-development.md).

---

## Authentication

Terrapod supports multiple authentication methods:

- **Local passwords** — PBKDF2-SHA256 hashed, with zxcvbn strength validation
- **OIDC** — Auth0, Okta, Azure AD, and any standards-compliant provider via authlib
- **SAML** — Azure AD SAML and other SAML 2.0 providers via python3-saml
- **terraform login** — OAuth2 Authorization Code with PKCE for CLI authentication
- **API tokens** — long-lived tokens for automation, SHA-256 hashed at rest

See [docs/authentication.md](docs/authentication.md) for setup guides.

---

## Documentation

[docs/index.md](docs/index.md) is the full index; [llms.txt](llms.txt) is the
machine-readable map for AI assistants. The ones most people want:

| Document | Description |
|---|---|
| [Architecture](docs/architecture.md) | System components, BFF pattern, storage, runners, auth flows |
| [Getting Started](docs/getting-started.md) | Deploy the Helm chart on Kubernetes (or k3s), first workspace, first plan/apply |
| [Migration](docs/migration.md) | Move a TFE / HCP Terraform or Atlantis platform onto Terrapod with `terrapod-migrate` — dry-run-first, reversible, what transfers vs. what's left as a checklist |
| [RBAC](docs/rbac.md) | Permission model, label-based access control, custom roles |
| [API Reference](docs/api-reference.md) | All API endpoints with examples |
| [Deployment](docs/deployment.md) | Production Helm deployment, storage backends, scaling |
| [Upgrading to 2.0](docs/upgrading-to-2.0.md) | The 2.0 migration notes — what needs an edit, the exact edit, and what does not break |
| [Versioning & Support](docs/versioning-and-support.md) | SemVer contract per surface, version-skew support, deprecation window, support matrix |
| [Known Limitations](docs/known-limitations.md) | What Terrapod does not (yet) do — deployment, scope, and feature constraints, stated plainly |
| [Production Checklist](docs/production-checklist.md) | Pre-go-live checklist for a production deployment |

---

## Building it

Everything runs in Docker; the Python side needs no local toolchain at all.
Frontend work additionally wants Node, for `npm run build`.

```zsh
make dev          # local dev environment (Tilt)
make test         # pytest, in Docker
make lint         # ruff check + format, in Docker
make pentest      # Semgrep (SAST) + Trivy (image CVEs) + Nuclei (DAST)
```

The stack is FastAPI + PostgreSQL + Redis on the server, Next.js on the front,
and Go for the SDK, provider, migration and publishing tools. Security scanning
is a standing part of the build rather than a release-time afterthought, and its
reports land in `reports/pentest/`.

[CONTRIBUTING.md](CONTRIBUTING.md) has the workflow, [SECURITY.md](SECURITY.md)
the disclosure policy, and [AGENTS.md](AGENTS.md) the architecture, contracts and
conventions — point your AI assistant at that one.

---

## Comparison with Alternatives

| Project | What it does | Position relative to Terrapod |
|---|---|---|
| [Terrakube](https://terrakube.io/) | Open-source TFC/TFE replacement | Closest peer — comparable full-platform scope (see below) |
| [OpenTofu](https://opentofu.org/) | Open-source Terraform fork (CLI) | The Terraform-compatible CLI engine; Terrapod runs it as an execution backend |
| [Atlantis](https://www.runatlantis.io/) | PR-based plan/apply automation | Battle-tested PR-driven plan/apply automation; pairs with the state, registry and RBAC tooling you already run |
| [Digger](https://digger.dev/) | CI-native Terraform orchestration | Runs inside your CI; deliberately no separate execution engine to operate |
| [Terrateam](https://terrateam.io/) | GitHub-integrated TF automation | GitHub-focused; open-core (community + paid tiers) |
| [Spacelift](https://spacelift.io/) | Commercial TF management platform | Vendor-supported commercial platform; evaluate it if a managed option fits |

### Terrakube

[Terrakube](https://terrakube.io/) is the closest open-source alternative and the project most worth comparing against. It is **also** a full self-hosted Terraform Cloud / Enterprise replacement: it implements the same `cloud {}` / `backend "remote"` TFE V2 API that Terrapod targets, and ships organizations, a private module + provider registry with GPG-signed provider publishing, granular RBAC, VCS integration (GitHub/GitLab/Bitbucket/Azure DevOps), dynamic provider credentials (AWS/GCP/Azure workload identity), OPA policy checks, and ephemeral Kubernetes-Job executors. It is Apache-2.0, built on Java/Spring Boot + Angular, with an established community and a frequent release cadence. If you are choosing a Terraform platform today it is worth evaluating alongside Terrapod: on the core surface the two are at rough parity, and Terrakube has the longer track record.

**Where Terrakube differs from Terrapod:**

- **Multi-organization tenancy** with teams. Terrapod is single-org by deliberate design — a choice aligned with [HashiCorp's own current guidance](https://developer.hashicorp.com/validated-patterns/terraform/migrate-terraform-orgs-projects), which now recommends *minimizing* organizations and consolidating onto one (segmenting internally instead). Terrapod's tenant boundary is the deployment: for separate tenants, run an instance per tenant; for segmentation within one company, label-based RBAC covers what projects/teams do. If you specifically need several named organizations behind a single endpoint, Terrakube offers that and Terrapod does not — see [Why a single organization](docs/architecture.md#why-a-single-organization).

**Where Terrakube is more mature:**

- **Maturity** — longer track record, larger community, more permissive (Apache-2.0) license. Terrapod is newer and backed by a small core team.

**Where Terrapod is genuinely differentiated.** The first three share one theme — Terrapod is built for restricted-network, multi-cluster, low-upstream-dependency topologies:

- **Firewall-friendly cross-cluster execution** — Terrapod runners connect *outbound* to the control plane over SSE and create Jobs locally, so the API holds no inbound reach and no Kubernetes access into the execution cluster. This suits isolated / NAT'd / outbound-only execution clusters. Terrakube integrates execution differently — a control-plane-coordinated executor model — a different network topology that fits different constraints.
- **Polling-first VCS** — Terrapod supports inbound webhooks (GitHub and GitLab) but does not require them: it also polls VCS over outbound HTTPS, so the integration works behind firewalls/NATs with no inbound delivery. Terrakube uses webhook delivery. Different fits for inbound-restricted networks.
- **Pull-through provider mirror + terraform/tofu binary cache** — runners have zero direct upstream dependency for cached platforms. Terrakube ships a local plugin cache.
- **Monorepo autodiscovery** — Atlantis-style auto-creation of workspaces from glob-matched directories on PRs. Terrakube approaches monorepos through directory filtering.
- **Run tasks** — pre/post-plan external webhook validation hooks.
- **Custom execution steps** — *execution hooks* run operator-supplied shell at five run-lifecycle points (`pre_init`, `pre_plan`, `post_plan`, `pre_apply`, `post_apply`) inside the runner Job, as reusable, workspace-associated library entries with priority ordering, fail-the-run semantics, audit logging, and a platform kill-switch. `pre_init` is the pre-`init` slot for installing extra tooling, authenticating to a secret backend, or fetching certs; heavier or image-level customization uses a custom runner image. See [docs/execution-hooks.md](docs/execution-hooks.md).
- **In-platform AI** — plan summaries, failure analysis, and chat (Terrakube integrates AI via an external MCP server).
- **Native Terragrunt** — a per-workspace flag wraps agent-mode runs in `terragrunt` (pull-through binary cache, local-backend reconciliation) while Terrapod keeps owning state and the run lifecycle; CLI-driven runs need no config. See [docs/terragrunt.md](docs/terragrunt.md).
- First-class OPA **policy sets** with mandatory/advisory enforcement, native multi-channel **notifications** (Slack/email/webhook), and cross-workspace **run triggers**.

In short: Terrapod's niche is **restricted-network, multi-cluster execution** — outbound-only runners, polling VCS, self-contained caching — with an AI-assisted review layer over the top. Terrakube has the longer track record and offers multi-org tenancy for those who want it; Terrapod is deliberately single-org, in line with [HashiCorp's current direction](https://developer.hashicorp.com/validated-patterns/terraform/migrate-terraform-orgs-projects). Pick on that basis.

Licensing: Terrapod is **MPL-2.0** (file-level copyleft, the same license as OpenTofu and the historical Terraform codebase); Terrakube is **Apache-2.0** (permissive) — relevant if you intend to redistribute a modified platform.

---

## License

[MPL-2.0](LICENSE) — file-level copyleft keeps Terrapod's own source open while staying friendly to enterprise adoption (the same license as OpenTofu and the historical Terraform codebase). For most operators this is a non-issue: **running Terrapod internally, self-hosted, imposes no obligation to disclose your own code or configuration** — MPL-2.0's copyleft is file-level (it attaches to modifications of Terrapod's *own* files you distribute), and there is no network-use trigger.

---

## Trademarks

Terrapod is not affiliated with, endorsed by, or a product of HashiCorp, Inc. or IBM. Terraform is a trademark of HashiCorp, Inc. OpenTofu is a project of the Linux Foundation.

---

## Contributing

Contributions are very welcome, AI-assisted ones included.
The platform core is Python, which keeps the contribution barrier low.

The short version: **start with an issue** (every change beyond a trivial tweak
gets one), branch from `main`, run the checks for what you changed (`make test`
for Python, `npm run build` for the frontend, `helm template …` for Helm), and
open a PR that references the issue.

- **[CONTRIBUTING.md](CONTRIBUTING.md)** — setup, the issue-first workflow, and how to open a PR.
- **[AGENTS.md](AGENTS.md)** — architecture, the API↔consumer and code↔tests contracts, and conventions. If you use an AI coding assistant, point it here.

Browse [`good first issue`](https://github.com/mattrobinsonsre/terrapod/labels/good%20first%20issue)
and [`help wanted`](https://github.com/mattrobinsonsre/terrapod/labels/help%20wanted)
for a place to start.

### Team

Terrapod is built and maintained by a small core team with site-reliability and
platform-engineering backgrounds. [@mattrobinsonsre](https://github.com/mattrobinsonsre) currently
leads the project; [@karl0r](https://github.com/karl0r) and
[@mhempstock](https://github.com/mhempstock) are maintainers. We'd welcome more
hands — start by contributing.
