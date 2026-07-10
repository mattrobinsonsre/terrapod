# Terrapod

[![CI](https://github.com/mattrobinsonsre/terrapod/actions/workflows/ci.yml/badge.svg)](https://github.com/mattrobinsonsre/terrapod/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

**A free, open-source, self-hosted platform replacement for Terraform Enterprise / HCP Terraform.**

Get the collaboration, governance, state, registry, and UI layer of a commercial Terraform platform — self-hosted on your own Kubernetes, with no per-resource licensing and nothing proprietary in the stack. Terrapod gives you label-based RBAC and OPA/Rego policy-as-code (the open-source equivalent of TFE's Sentinel), versioned remote state, a private module + provider registry, and a modern web UI — all wrapped around `terraform` or `tofu`. It targets compatibility with the [TFE V2 API](https://developer.hashicorp.com/terraform/enterprise/api-docs), so existing `cloud` blocks, [`go-tfe`](https://pkg.go.dev/github.com/hashicorp/go-tfe) clients, and CI/CD point at a Terrapod instance with minimal reconfiguration — usually zero code changes. It's **GPLv3**, and self-hosted internal use triggers no source-disclosure obligation (GPLv3 is not AGPL).

Terrapod is **not** a fork of Terraform or OpenTofu. It orchestrates them.

![Terrapod run detail — plan output with an AI change summary, per-policy OPA pass/fail, and resource-change cards](docs/images/run-detail.png)
*A single run: plan output, an AI-generated change summary and risk assessment, per-policy OPA results, and resource-change cards.*

**Which path are you on?** — [Evaluate](#quick-evaluation) (`make eval`, one command) · [Deploy](#quick-start) (Helm on your cluster) · [Migrate](docs/migration.md) (off TFE / HCP / Atlantis, reversible) · [Contribute](CONTRIBUTING.md)

---

## Coming from Terraform Enterprise / HCP Terraform?

Terrapod targets API compatibility with the surface the `terraform`/`tofu` CLI and `go-tfe` consume, so most tooling points at it unchanged. Where the *models* differ (structural facts, not a feature-by-feature scorecard):

| | HCP Terraform / TFE | Terrapod |
|---|---|---|
| Hosting | Vendor SaaS, or a self-managed distribution | Self-hosted on your own Kubernetes |
| Licensing & cost | Proprietary (BUSL), priced by managed resources | Free and open source (GPLv3) |
| Where state + secrets live | On the vendor / self-managed control plane | Never leave your boundary (your Postgres + object store) |
| Cloud credentials | Vendor-stored or dynamic | K8s workload identity (IRSA / WIF / Azure WI) — nothing long-lived |
| Policy engine | Sentinel (proprietary) | OPA / Rego (open) — advisory or mandatory |
| Restricted-network / air-gap execution | SaaS-dependent by default | First-class — outbound-only runners, polling VCS, pull-through mirror + sealed cache-only mode |
| Private registry · RBAC · SSO · Audit | Yes | Yes — self-hosted equivalents (label RBAC; OIDC/SAML; immutable audit) |
| Multi-organization | Yes | Single org by design ([run an instance per tenant](docs/architecture.md#why-a-single-organization)) |
| CLI / `go-tfe` API | TFE V2 | TFE V2-compatible |

*Model-level comparison — it deliberately avoids version-specific pricing and fast-moving feature claims; confirm current HCP terms with HashiCorp.*

> **Ready to move?** [`terrapod-migrate`](docs/migration.md) does it dry-run-first and fully reversible — preview every workspace, variable, variable set, VCS connection, state file (serial + lineage preserved), run trigger, notification, agent pool, and registry signing key it will create, apply, verify parity, then roll back cleanly if needed. Registry module/provider *versions* are reported for re-publish rather than auto-created (a source-API + signing-key limitation, [documented plainly](docs/migration.md#what-actually-transfers-today)).

---

## Is Terrapod for you?

If you want an open, self-hosted alternative to Terraform Enterprise / HCP Terraform, yes. Terrapod is the **full** platform layer, not a thin slice of it — point your existing `cloud` blocks, `go-tfe` clients, and CI/CD at it and it just works, with zero code changes.

Everything you'd expect from the platform tier is here and first-class:

- Workspaces with versioned remote state (locking, rollback), the full plan → apply run lifecycle, and both VCS-driven and CLI-driven runs.
- A GPG-signed private module + provider registry, variables & variable sets, run triggers, notifications, drift detection, and a polished, mobile-friendly web UI.
- Label-based RBAC and OPA/Rego policy-as-code (the open-source equivalent of Sentinel) — advisory or mandatory.
- API compatibility with the TFE V2 surface the `terraform`/`tofu` CLI and `go-tfe` speak, so most tooling points at it unchanged.

And a few standout, first-class features you may really like:

- **Runs anywhere your network is awkward.** Runners dial *out* and create Kubernetes Jobs locally, so the control plane never needs inbound reach into an execution cluster — isolated VPCs, other regions, on-prem, or behind egress-only firewalls. VCS is polled outbound, and a pull-through provider mirror + binary cache (with an air-gap sealed mode) lets runs resolve providers and binaries with no upstream internet.
- **Zero static cloud credentials.** Runs and the platform reach cloud APIs through Kubernetes workload identity (AWS IRSA, GCP WIF, Azure WI) — nothing long-lived to store, leak, or rotate.
- **An AI-augmented review layer** — optional and off by default — plan change-summaries, risk assessment, failure analysis, and a chat to interrogate a run.
- **A reversible, dry-run-first migration** off TFE / HCP Terraform / Atlantis with [`terrapod-migrate`](docs/migration.md) — preview everything, apply, verify parity, and roll back cleanly.

The one hard requirement is Kubernetes, and that's a low bar: Terrapod is a single Helm release, a one-node [k3s](https://k3s.io/) VM is plenty to start, and `make eval` spins up a throwaway [k3d](https://k3d.io/)/kind cluster in one command.

Three deliberate design foci set Terrapod apart, each with a doc to go deeper: **restricted-network & multi-cluster execution** (outbound-only runners + polling VCS + a self-contained provider mirror/binary cache with a sealed air-gap mode — see [network isolation](docs/deployment-network-isolation.md) and the [ARC execution model](docs/architecture.md#runner-architecture-arc-pattern)); an **AI-augmented review layer** (provider-agnostic via [LiteLLM](https://github.com/BerriAI/litellm), off by default — see [AI plan summary](docs/ai-plan-summary.md)); and a **low contribution barrier** (a Python platform core, AI-assisted contributions welcome — see [`llms.txt`](llms.txt) and [AGENTS.md](AGENTS.md)).

---

## Enterprise-ready

Built to run in production, not just to demo — the de-risking signals in one place:

- **High availability, no leader election** — run the API and listeners multi-replica behind a load balancer; all background work coordinates through Redis, so any replica can do any job and nothing is a single point of failure. PodDisruptionBudgets are on by default. See [Architecture → distributed scheduler](docs/architecture.md#distributed-task-scheduler).
- **Enterprise identity & access** — SSO via OIDC and SAML (Auth0, Okta, Azure AD, …), plus `terraform login` (OAuth2 + PKCE) and long-lived API tokens for automation; granular label-based RBAC with `resource:verb` capabilities. See [Authentication](docs/authentication.md) · [RBAC](docs/rbac.md).
- **Immutable audit** — a tamper-evident, retention-configurable audit log of every API action. See [Audit logging](docs/audit-logging.md).
- **Hardened by default** — every pod runs non-root, read-only root filesystem, all capabilities dropped, and a seccomp profile. See [Security hardening](docs/security-hardening.md) · [Production checklist](docs/production-checklist.md).
- **Verifiable supply chain** — every release image and the Helm chart is keyless-signed with cosign and carries SBOM (SPDX) + SLSA build-provenance; verify with `cosign verify` / `gh attestation verify` before you deploy. See [Supply-chain verification](docs/supply-chain-verification.md).
- **Backup & disaster recovery** — an optional shipped `pg_dump` backup CronJob, a restore-verification DR drill (a real green check, not a doc), and documented break-glass state recovery straight from object storage. See [Disaster recovery](docs/disaster-recovery.md).
- **Reversible upgrades & migration** — every schema migration ships a real `upgrade()`/`downgrade()` and the chart is the single upgrade unit, so version bumps are auditable and reversible; migrating *in* off TFE / HCP / Atlantis is dry-run-first and roll-back-able. See [Deployment](docs/deployment.md) · [Migration](docs/migration.md).

---

## Quick Evaluation

Try Terrapod end-to-end on your laptop in one command. It spins up a throwaway
[kind](https://kind.sigs.k8s.io/) or [k3d](https://k3d.io/) cluster and installs
a complete, self-contained stack — in-cluster PostgreSQL + Redis, filesystem
storage, a local admin login — with no cloud account and no external
dependencies. It even seeds a sample workspace + a completed plan, so you land
on a populated UI, not an empty list:

![Terrapod eval walkthrough — login, sample workspace, and a completed plan](docs/images/eval-demo.gif)

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

## Features

Everything below is implemented and shipped today.

### Core platform

| Feature | Description |
|---|---|
| Workspaces | Isolate state, variables, and runs per workspace |
| Remote state | Versioned state with locking and rollback; encrypted at rest by your object store, with optional app-layer BYOK envelope encryption |
| CLI-driven runs | `terraform` / `tofu` plan / apply via the `cloud` backend (both verified) |
| Agent execution | Server-side plan / apply on ephemeral K8s Jobs (ARC pattern) |
| Agent pools | Named runner-listener groups; join-token → certificate exchange for auth |
| TFE V2 API | JSON:API surface compatible with `go-tfe` / `terraform login` |
| Run triggers | Cross-workspace dependency chains — a source apply triggers downstream runs |
| Stale-plan guards | Auto-discard a plan that no longer reflects reality: state-version drift (always on) + optional per-workspace time-based plan expiry |

### Governance & security

| Feature | Description |
|---|---|
| Label-based RBAC | Roles with granular `resource:verb` capabilities (e.g. `run:plan` without `run:apply`); read/plan/write/admin levels remain as authoring shorthand |
| Policy-as-code (OPA) | Rego enforcement on plan JSON — the open-source equivalent of Sentinel. Advisory or mandatory sets, label-scoped to workspaces, evaluated on the runner, with admin override |
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
| Sealed (cache-only) mode | Air-gap switch (`registry.cache_only`) guaranteeing no upstream fetch — cache-backed version resolution, actionable cache-miss errors, retention skips the caches |

### Integrations & operations

| Feature | Description |
|---|---|
| VCS integration | GitHub App + GitLab token; inbound webhooks supported (GitHub HMAC + GitLab token) for instant triggers, with outbound polling as the resilient default — so webhooks are optional, never required |
| Workspace autodiscovery | Atlantis-style monorepo autodiscovery — pattern-matched rules auto-create workspaces on PRs to new directories |
| Terragrunt | Per-workspace Terragrunt for agent-mode runs (a flag + pinned version, pull-through binary cache, local-backend reconciliation so Terrapod still owns state); CLI-driven runs need no extra config |
| Variables & secrets | Per-workspace env and Terraform variables; sensitive values protected by database encryption-at-rest; variable sets |
| Drift detection | Scheduled plan-only runs to detect out-of-band changes, with a per-workspace ignore allowlist |
| Notifications | Webhook (HMAC-SHA512), Slack (Block Kit), and email alerts on run events |
| Interactive Slack app | Outbound Socket Mode app — `/terrapod` account linking + opt-in per-workspace run notifications with RBAC-checked Approve/Discard buttons; multiple deployments can share one Slack workspace |
| Run tasks | Pre/post-plan webhook hooks for external validation |
| Execution hooks | Admin-managed custom shell steps run in the runner Job at pre_init / pre_plan / post_plan / pre_apply / post_apply, associated with workspaces |
| Service catalog | No-code self-service provisioning over the module registry |
| Impact graph | Interactive dependency + blast-radius view of a plan on the run page — module-clustered, click a resource to light up its transitive downstream impact |
| Workspace health | Per-workspace health conditions, VCS polling status, drift detection indicators |

### AI (optional, off by default)

| Feature | Description |
|---|---|
| AI plan review | LLM change summary + risk assessment on every plan, failure analysis on errored plans, and a chat to interrogate a run — provider-agnostic via LiteLLM (AWS Bedrock, OpenAI, Anthropic, Gemini, Azure OpenAI, vLLM). IAM-native auth for Bedrock (IRSA + optional cross-account `sts:AssumeRole`) |

</details>

### More screenshots

<details>
<summary>Workspace overview, variables, and agent pools</summary>

![Workspace overview with VCS integration, drift detection, and labels](docs/images/workspace-overview.png)
![Variables with sensitive masking and HCL support](docs/images/workspace-variables.png)
![Agent pools with listener health monitoring](docs/images/admin-agent-pools.png)
![Impact graph: a plan clustered by module with a resource's downstream blast radius highlighted](docs/images/impact-graph.png)
</details>

---

## Architecture

```
                              +---------------------+
                              |     Browser / CLI    |
                              +----------+----------+
                                         |
                                     HTTPS (TLS)
                                         |
                              +----------v----------+
                              |      Ingress         |
                              +----------+----------+
                                         |
                              +----------v----------+
                              |   Next.js Frontend   |  (BFF pattern)
                              |   (Web UI + Proxy)   |
                              +----+------------+---+
                                   |            |
                        /app/*     |            |  /api/*  /.well-known/*
                        (pages)    |            |  (rewrite to API)
                                   |            |
                              +----v------------v---+
                              |   FastAPI API Server |
                              +--+------+------+----+
                                 |      |      |
                    +------------+   +--+--+   +------------+
                    |                |     |                 |
              +-----v-----+  +-----v-+ +-v----------+ +----v-------+
              | PostgreSQL |  | Redis | | Object     | | VCS Polls  |
              | (data,     |  | (sess | | Storage    | | (GitHub,   |
              |  state     |  |  ions,| | (S3/Azure/ | |  GitLab)   |
              |  metadata) |  |  locks| |  GCS/FS)   | +------------+
              +-----------+   +------+  +-----------+
                                              ^
                              +---------------+
                              |               |
                    +---------v----------+    |
                    |  Runner Listener   |    |  (one or more, each
                    |  (K8s Deployment,  |    |   joins a pool via
                    |   joins pool via   |    |   join token)
                    |   join token)      |    |
                    +---------+----------+    |
                              |               |
                    +---------v----------+    |
                    |  K8s Jobs          |    |
                    |  (ephemeral        |    |
                    |   terraform/tofu)  |    |
                    +--------------------+    +
```

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

- A Kubernetes cluster (1.27+). No cluster? `curl -sfL https://get.k3s.io | sh -` gives you one on a single VM, with an ingress controller (Traefik) and storage included.
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

| Document | Description |
|---|---|
| [Architecture](docs/architecture.md) | System components, BFF pattern, storage, runners, auth flows |
| [Getting Started](docs/getting-started.md) | Deploy the Helm chart on Kubernetes (or k3s), first workspace, first plan/apply |
| [Migration](docs/migration.md) | Move a TFE / HCP Terraform or Atlantis platform onto Terrapod with `terrapod-migrate` — dry-run-first, reversible, what transfers vs. what's left as a checklist |
| [Local Development](docs/local-development.md) | Run Terrapod from source with Tilt (contributors only) |
| [Authentication](docs/authentication.md) | Local auth, OIDC, SAML, terraform login, API tokens |
| [RBAC](docs/rbac.md) | Permission model, label-based access control, custom roles |
| [API Reference](docs/api-reference.md) | All API endpoints with examples |
| [Deployment](docs/deployment.md) | Production Helm deployment, storage backends, scaling |
| [Registry](docs/registry.md) | Private module/provider registry, caching layers |
| [Registry Publishing](docs/registry-publishing.md) | Publishing providers/modules with `terrapod-publish` and the client-signed publish protocol |
| [VCS Integration](docs/vcs-integration.md) | GitHub and GitLab setup, polling, webhooks |
| [VCS Workflows](docs/vcs-workflows.md) | PR/MR comment commands, speculative plans, apply-on-merge |
| [Policies (OPA)](docs/policies.md) | Rego policy authoring, advisory vs mandatory enforcement, label-based scoping, admin override |
| [Autodiscovery](docs/autodiscovery.md) | Atlantis-style monorepo workspace autodiscovery |
| [Drift Detection](docs/drift-detection.md) | Scheduled plan-only runs to detect infrastructure drift |
| [Drift Ignore Rules](docs/drift-ignore-rules.md) | Suppress known/expected drift by resource address or attribute |
| [Run Triggers](docs/run-triggers.md) | Cross-workspace dependency chains |
| [Terragrunt](docs/terragrunt.md) | CLI-driven and agent-mode Terragrunt support |
| [Remote State](docs/remote-state.md) | State versioning, locking, rollback, the `cloud` backend |
| [AI Plan Summary](docs/ai-plan-summary.md) | LLM plan summaries, risk assessment, failure analysis, chat |
| [Impact Graph](docs/impact-graph.md) | Interactive dependency + blast-radius view of a plan on the run page, clustered by module |
| [Notifications](docs/notifications.md) | Webhook, Slack, and email alerts on run events |
| [Slack Integration](docs/slack-integration.md) | Interactive Socket Mode app — account linking, approvals, run notifications |
| [Run Tasks](docs/run-tasks.md) | Pre/post-plan webhook hooks for external validation |
| [Execution Hooks](docs/execution-hooks.md) | Custom shell steps run in the runner Job at pre_init/pre_plan/post_plan/pre_apply/post_apply, associated with workspaces |
| [Audit Logging](docs/audit-logging.md) | Immutable event log, query API, retention |
| [Artifact Retention](docs/artifact-retention.md) | Retention + purge of run logs, plans, and config tarballs |
| [Runners](docs/runners.md) | Agent pools, the listener/runner ARC model, custom runner images |
| [Cloud Credentials](docs/cloud-credentials.md) | AWS IRSA, GCP WIF, Azure WI setup + a preflight doctor that verifies SA→role + object-store access before the first run |
| [Service Catalog](docs/service-catalog.md) | No-code self-service provisioning over the module registry |
| [Monitoring](docs/monitoring.md) | Prometheus metrics, scraping, shipped Grafana dashboard + alert rules (with per-alert runbooks) |
| [Optional Webhook Ingress](docs/deployment-webhook-ingress.md) | Split public webhook ingress so the management plane can stay private |
| [Forward Proxy & Custom CA](docs/deployment-proxy.md) | Route all outbound HTTP(S) through a corporate proxy and trust a private/MITM CA, across every component including runner Jobs |
| [Security Hardening](docs/security-hardening.md) | Pod hardening defaults, secrets, network posture |
| [Supply-chain Verification](docs/supply-chain-verification.md) | Verify Terrapod's own signed images + SBOM/SLSA attestations, and how cached binaries/providers are verified against publisher signatures |
| [Known Limitations](docs/known-limitations.md) | What Terrapod does not (yet) do — deployment, scope, and feature constraints, stated plainly |
| [Production Checklist](docs/production-checklist.md) | Pre-go-live checklist for a production deployment |
| [Disaster Recovery](docs/disaster-recovery.md) | Break-glass state recovery, shipped DB backup CronJob + restore-verification DR drill, per-backend object-storage protection |
| [Encryption at Rest](docs/encryption-at-rest.md) | Optional off-by-default app-layer (BYOK) envelope encryption of DB secrets and state files — for no-/niche-CSP, bare-metal, or air-gapped deployments (static / Vault Transit / AWS KMS) |

---

## Tech Stack

| Layer | Technology |
|---|---|
| API server | Python 3.13+ / FastAPI / SQLAlchemy (async) / Pydantic |
| Database | PostgreSQL |
| Cache / Sessions | Redis |
| Object storage | AWS S3, Azure Blob, GCS, or filesystem (native SDKs) |
| Frontend | Next.js 16 / React 19 / TypeScript / Tailwind CSS / Radix UI (responsive, mobile-first) |
| Runner listener | Python (same codebase as API) |
| Auth | authlib (OIDC), python3-saml (SAML) |
| Deployment | Helm chart on Kubernetes |
| CI | GitHub Actions |

---

## Development

All builds, tests, and linting run in Docker — no local Python or Node.js install needed.

```zsh
make dev          # Start local dev environment (Tilt)
make dev-down     # Stop local dev environment
make test         # Run pytest in Docker (with LocalStack for S3)
make lint         # Run ruff (check + format) in Docker
make images       # Build production Docker images
```

### Conventions

- **Issue-first** — every change beyond a trivial tweak starts with a GitHub issue; the PR references it (`closes #N`)
- **Commits** — conventional commits (`feat:`, `fix:`, `docs:`, `chore:`)
- **Branches** — feature branches off `main`; never push directly to `main`
- **API contract** — JSON:API spec; compatibility tested against the `go-tfe` client
- **Migrations** — Alembic with async SQLAlchemy
- **Local dev** — Tilt with live_update for Python and Node.js hot reload

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow and [AGENTS.md](AGENTS.md) for the architecture, contracts, and conventions (point your AI assistant at it).

---

## Security Testing

Terrapod includes a three-layer pen testing framework. All tools run in Docker.

```zsh
make pentest-sast     # Static analysis (Semgrep)
make pentest-images   # Container image CVE scan (Trivy)
make pentest-dast     # Dynamic testing against live stack (Nuclei)
make pentest          # All three layers
```

| Layer | Tool | What it covers |
|---|---|---|
| SAST | [Semgrep](https://semgrep.dev/) | OWASP Top 10, secrets detection, project-specific rules (naive datetimes, raw background tasks) |
| Container scanning | [Trivy](https://trivy.dev/) | HIGH/CRITICAL CVEs in the `terrapod-api`, `terrapod-web`, and `terrapod-runner` images |
| DAST | [Nuclei](https://nuclei.projectdiscovery.io/) | Auth bypass, header injection, CORS validation, state endpoint security, HTTP method restriction |

Reports are written to `reports/pentest/`. See [SECURITY.md](SECURITY.md) for the full security policy.

---

## Comparison with Alternatives

| Project | What it does | Position relative to Terrapod |
|---|---|---|
| [Terrakube](https://terrakube.io/) | Open-source TFC/TFE replacement | Closest peer — comparable full-platform scope (see below) |
| [OpenTofu](https://opentofu.org/) | Open-source Terraform fork (CLI) | CLI only — no collaboration platform; Terrapod runs it as an engine |
| [Atlantis](https://www.runatlantis.io/) | PR-based plan/apply automation | No UI, no state management, no registry, no RBAC |
| [Digger](https://digger.dev/) | CI-native Terraform orchestration | Runs inside CI; no standalone platform |
| [Terrateam](https://terrateam.io/) | GitHub-integrated TF automation | GitHub-coupled; limited community edition |
| [Spacelift](https://spacelift.io/) | Commercial TF management platform | Not open source |

### Terrakube

[Terrakube](https://terrakube.io/) is the closest open-source alternative and the project most worth comparing against. It is **also** a full self-hosted Terraform Cloud / Enterprise replacement: it implements the same `cloud {}` / `backend "remote"` TFE V2 API that Terrapod targets, and ships organizations, a private module + provider registry with GPG-signed provider publishing, granular RBAC, VCS integration (GitHub/GitLab/Bitbucket/Azure DevOps), dynamic provider credentials (AWS/GCP/Azure workload identity), OPA policy checks, and ephemeral Kubernetes-Job executors. It is Apache-2.0, built on Java/Spring Boot + Angular, with an established community and a frequent release cadence. **If you are choosing a Terraform platform today, evaluate Terrakube alongside Terrapod** — on the core surface the two are at rough parity, and Terrakube is the more mature project.

**Where Terrakube differs from Terrapod:**

- **Multi-organization tenancy** with teams. Terrapod is single-org by deliberate design — a choice aligned with [HashiCorp's own current guidance](https://developer.hashicorp.com/validated-patterns/terraform/migrate-terraform-orgs-projects), which now recommends *minimizing* organizations and consolidating onto one (segmenting internally instead). Terrapod's tenant boundary is the deployment: for separate tenants, run an instance per tenant; for segmentation within one company, label-based RBAC covers what projects/teams do. If you specifically need several named organizations behind a single endpoint, Terrakube offers that and Terrapod does not — see [Why a single organization](docs/architecture.md#why-a-single-organization).

**Where Terrakube is more mature:**

- **Maturity** — longer track record, larger community, more permissive (Apache-2.0) license. Terrapod is newer and backed by a small core team.

**Where Terrapod is genuinely differentiated.** The first three share one theme — Terrapod is built for restricted-network, multi-cluster, low-upstream-dependency topologies:

- **Firewall-friendly cross-cluster execution** — Terrapod runners connect *outbound* to the control plane over SSE and create Jobs locally, so the API holds no inbound reach and no Kubernetes access into the execution cluster. This suits isolated / NAT'd / outbound-only execution clusters. Per Terrakube's documented model, its API reaches into the executor (so the executor is exposed to the control plane) — a different network topology, better fit for different constraints.
- **Polling-first VCS** — Terrapod supports inbound webhooks (GitHub and GitLab) but does not require them: it also polls VCS over outbound HTTPS, so the integration works behind firewalls/NATs with no inbound delivery. Terrakube uses webhook delivery. Different fits for inbound-restricted networks.
- **Pull-through provider mirror + terraform/tofu binary cache** — runners have zero direct upstream dependency; Terrakube ships a local plugin cache.
- **Monorepo autodiscovery** — Atlantis-style auto-creation of workspaces from glob-matched directories on PRs (Terrakube has directory filtering, but not auto-creation).
- **Run tasks** — pre/post-plan external webhook validation hooks (not present in Terrakube).
- **In-platform AI** — plan summaries, failure analysis, and chat (Terrakube integrates AI via an external MCP server).
- **Native Terragrunt** — a per-workspace flag wraps agent-mode runs in `terragrunt` (pull-through binary cache, local-backend reconciliation) while Terrapod keeps owning state and the run lifecycle; CLI-driven runs need no config. See [docs/terragrunt.md](docs/terragrunt.md).
- Additionally — first-class OPA **policy sets** with mandatory/advisory enforcement, native multi-channel **notifications** (Slack/email/webhook), and cross-workspace **run triggers**.

Net: Terrapod is not a "better general TFE replacement" — Terrakube is the more mature project and offers multi-org tenancy for those who want it (Terrapod is deliberately single-org, in line with [HashiCorp's current direction](https://developer.hashicorp.com/validated-patterns/terraform/migrate-terraform-orgs-projects)). Terrapod's defensible niche is **restricted-network, multi-cluster execution** (outbound-only runners, polling VCS, self-contained caching) with an AI-assisted review layer. Pick on that basis.

Licensing: Terrapod is **GPLv3** (strong copyleft); Terrakube is **Apache-2.0** (permissive) — relevant if you intend to redistribute a modified platform.

---

## License

[GPLv3](LICENSE) — strong copyleft keeps Terrapod and all derivative works open source. For most operators this is a non-issue: **running Terrapod internally, self-hosted, imposes no obligation to disclose your own code or configuration** — GPLv3's copyleft attaches to *distributing a modified Terrapod*, and GPLv3 is not AGPL (there is no network-use trigger).

---

## Trademarks

Terrapod is not affiliated with, endorsed by, or a product of HashiCorp, Inc. or IBM. Terraform is a trademark of HashiCorp, Inc. OpenTofu is a project of the Linux Foundation.

---

## Contributing

Contributions are very welcome — including AI-assisted ("vibe") contributions.
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
platform-engineering backgrounds — a platform built by the kind of people who
operate it. [@mattrobinsonsre](https://github.com/mattrobinsonsre) currently
leads the project; [@karl0r](https://github.com/karl0r) and
[@mhempstock](https://github.com/mhempstock) are maintainers. We'd welcome more
hands — start by contributing.
