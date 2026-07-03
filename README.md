# Terrapod

[![CI](https://github.com/mattrobinsonsre/terrapod/actions/workflows/ci.yml/badge.svg)](https://github.com/mattrobinsonsre/terrapod/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

**Open-source platform replacement for Terraform Enterprise.**

Terrapod provides the collaboration, governance (label-based RBAC **and OPA/Rego policy-as-code** — the open-source equivalent of TFE's Sentinel), state management, and UI layer that wraps around `terraform` or `tofu` as pluggable execution backends. It targets API compatibility with the [HCP Terraform / TFE V2 API](https://developer.hashicorp.com/terraform/enterprise/api-docs) so that existing tooling -- the `terraform` CLI with `cloud` block, the [`go-tfe`](https://pkg.go.dev/github.com/hashicorp/go-tfe) client, CI/CD integrations -- can point at a Terrapod instance with minimal reconfiguration.

Terrapod is **not** a fork of Terraform or OpenTofu. It orchestrates them.

![Workspaces](docs/images/workspaces.png)

## Why Terrapod

Beyond broad TFE compatibility, Terrapod is built with three deliberate design foci:

- **Restricted-network & multi-cluster execution.** Runner listeners connect *outbound* over SSE and create Kubernetes Jobs locally, so the API never needs inbound reach into the clusters where runs happen — they can sit in isolated VPCs, other regions, or behind firewalls. VCS is polling-first (webhooks optional), and a pull-through provider mirror + CLI binary cache — with an air-gap **sealed mode** — let runners resolve providers and binaries with no upstream internet for cached platforms. See [docs/deployment-network-isolation.md](docs/deployment-network-isolation.md) and the [ARC execution model](docs/architecture.md#runner-architecture-arc-pattern).
- **An AI-augmented review layer.** Every plan can carry an LLM-generated change summary and risk assessment, with failure analysis on errored plans and a chat to interrogate a run — provider-agnostic via LiteLLM, and **disabled by default** for AI-averse deployments. See [docs/ai-plan-summary.md](docs/ai-plan-summary.md).
- **A low contribution barrier.** The platform core is **Python** (FastAPI + async SQLAlchemy), so the surface most changes touch is approachable; the consumer ecosystem (Go SDK, Terraform provider, migration/publish CLIs) is **Go**. AI-assisted contributions are welcome — point your assistant at [`llms.txt`](llms.txt) and [AGENTS.md](AGENTS.md), then see [CONTRIBUTING.md](CONTRIBUTING.md).

---

> **Drop-in replacement for HCP Terraform.** Point your existing `cloud` blocks, `go-tfe` clients, and CI/CD pipelines at Terrapod — zero code changes required.

> **AI-augmented plans.** Every plan can carry an LLM-generated change description, risk assessment, and (on failure) suggested fixes — provider-agnostic via [LiteLLM](https://github.com/BerriAI/litellm). Wire AWS Bedrock (Claude, Nova, gpt-oss) with native IAM auth, or point at OpenAI, Anthropic, Gemini, Azure OpenAI, or any OpenAI-compatible endpoint. See [docs/ai-plan-summary.md](docs/ai-plan-summary.md).

> **Policy-as-code with OPA.** Block applies on policy violations using [Open Policy Agent](https://www.openpolicyagent.org/) and the Rego language — the open-source equivalent of TFE's proprietary Sentinel. Policy sets are scoped to workspaces with the same label-based model as roles, evaluated on the runner against the plan JSON, and gated as `advisory` (warn) or `mandatory` (block). See [docs/policies.md](docs/policies.md).

> **Zero static cloud credentials, end to end.** Both the runs and the platform reach cloud APIs through Kubernetes workload identity — AWS IRSA, GCP Workload Identity Federation, Azure Workload Identity — so there are no long-lived access keys to store, leak, or rotate. Credentials are short-lived, auto-rotated, and auditable. See [docs/cloud-credentials.md](docs/cloud-credentials.md).

> **Contributions welcome — including AI-assisted ones.** The platform core is **Python** (FastAPI + async SQLAlchemy), which keeps the contribution barrier low; the consumer ecosystem (Go SDK, Terraform provider, migration/publish CLIs) is **Go**. Pairing with an AI coding assistant? Point it at [`llms.txt`](llms.txt) (a machine-friendly map of the repo, docs, and how to enable each feature) and [AGENTS.md](AGENTS.md) (architecture, contracts, conventions, and the hard invariants). Then read [CONTRIBUTING.md](CONTRIBUTING.md) and [open an issue](https://github.com/mattrobinsonsre/terrapod/issues) to get started.

---

## ⚡ Quick Evaluation

Try Terrapod end-to-end on your laptop in one command. It spins up a throwaway
[kind](https://kind.sigs.k8s.io/) or [k3d](https://k3d.io/) cluster and installs
a complete, self-contained stack — in-cluster PostgreSQL + Redis, filesystem
storage, a local admin login — with **no cloud account and no external
dependencies**.

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

## Key Features

| Feature | Status | Description |
|---|---|---|
| Workspaces | Implemented | Isolate state, variables, and runs per workspace |
| Remote State Management | Implemented | Versioned state storage with locking, rollback, encryption at rest via CSP services |
| Agent Execution | Implemented | Plan/apply runs on the server via K8s Job-based runner infrastructure |
| VCS Integration | Implemented | GitHub (App) and GitLab (access token); inbound webhooks supported (GitHub HMAC + GitLab token) for instant triggers, with outbound polling as the resilient default so webhooks are optional, never required |
| Variables & Secrets | Implemented | Per-workspace env and Terraform variables; sensitive values protected by database encryption-at-rest; variable sets |
| RBAC | Implemented | Label-based roles with granular capabilities (`resource:verb`, e.g. `run:plan` without `run:apply`); permission levels (read/plan/write/admin) remain as authoring shorthand |
| Private Module Registry | Implemented | Publish, version, and share modules internally |
| Private Provider Registry | Implemented | Publish, version, and share providers with GPG signing and network mirror caching |
| Binary Caching | Implemented | Pull-through cache for terraform/tofu/terragrunt CLI binaries |
| Cache Pre-population | Implemented | Seed the binary + provider caches ahead of time via a bulk-warm admin endpoint + UI panel (for restricted-network / fast-first-run deployments) |
| Sealed (cache-only) Mode | Implemented | Air-gap switch (`registry.cache_only`) guaranteeing no upstream fetch — cache-backed version resolution, actionable cache-miss errors, retention skips the caches |
| Supply-chain Verification | Implemented | Cached binaries + provider archives verified against the publisher's GPG-signed SHA256SUMS (pinned keys); the runner re-verifies the executable before running it |
| Signed Releases (cosign) | Implemented | Every release image + the Helm chart is keyless-signed with cosign, with per-image SBOM (SPDX) + SLSA build-provenance attestations — verifiable with `cosign verify` / `gh attestation verify`. See [docs/supply-chain-verification.md](docs/supply-chain-verification.md#verifying-terrapods-own-release-artifacts) |
| **Terragrunt** | **Implemented** | **Per-workspace Terragrunt support for agent-mode runs — a `terragrunt_enabled` flag + pinned version, pull-through binary cache for the terragrunt CLI, and transparent local-backend reconciliation so Terrapod still owns state. CLI-driven runs work with zero extra config. See [docs/terragrunt.md](docs/terragrunt.md).** |
| Agent Pools | Implemented | Named groups of runner listeners; join token → certificate exchange for auth |
| CLI-Driven Runs | Implemented | `terraform plan` / `apply` via cloud backend (both `terraform` and `tofu` verified) |
| TFE V2 API | Implemented | JSON:API surface compatible with `go-tfe` / `terraform login` |
| Audit Logging | Implemented | Immutable event log with configurable retention |
| SSO (OIDC / SAML) | Implemented | Pluggable identity providers (Auth0, Okta, Azure AD, etc.) |
| Drift Detection | Implemented | Scheduled plan-only runs to detect out-of-band changes |
| Run Triggers | Implemented | Cross-workspace dependency chains — source apply triggers downstream runs |
| Stale-plan Guards | Implemented | Auto-discard a plan that no longer reflects reality: state-version drift (always on) + optional per-workspace time-based plan expiry |
| **AI Plan Summary** | **Implemented** | **LLM-generated change summary + risk assessment on every plan; failure analysis on errored plans. Provider-agnostic via LiteLLM — AWS Bedrock (Claude, Nova, gpt-oss…), OpenAI, Anthropic direct, Google Gemini, Azure OpenAI, vLLM. IAM-native auth for Bedrock (IRSA + optional cross-account `sts:AssumeRole`).** |
| **Policy-as-Code (OPA)** | **Implemented** | **Rego-based policy enforcement on plan output — the open-source equivalent of Sentinel. Advisory or mandatory sets, label-scoped to workspaces, evaluated on the runner against plan JSON, with admin-override on mandatory blocks. Author Rego, attach to workspaces by label, see pass/fail per policy on every run.** |
| Notifications | Implemented | Webhook (HMAC-SHA512), Slack (Block Kit), and email alerts on run events |
| Run Tasks | Implemented | Pre/post-plan webhook hooks for external validation |
| Execution Hooks | Implemented | Admin-managed custom shell steps run in the runner Job at pre_init/pre_plan/post_plan/pre_apply/post_apply, associated with workspaces (Helm kill-switch `runners.hooksEnabled`) |
| Workspace Health | Implemented | Per-workspace health conditions, VCS polling status, drift detection indicators |
| Workspace Autodiscovery | Implemented | Atlantis-style monorepo autodiscovery — pattern-matched rules auto-create workspaces on PRs to new directories |
| Cloud Credentials | Implemented | Dynamic provider credentials via K8s workload identity (AWS IRSA, GCP WIF, Azure WI) |

### Screenshots

<details>
<summary>Workspace overview with VCS integration, drift detection, and labels</summary>

![Workspace Overview](docs/images/workspace-overview.png)
</details>

<details>
<summary>Run detail with plan output and VCS metadata</summary>

![Run Detail](docs/images/run-detail.png)
</details>

<details>
<summary>Variables with sensitive masking and HCL support</summary>

![Variables](docs/images/workspace-variables.png)
</details>

<details>
<summary>Agent pools with listener health monitoring</summary>

![Agent Pools](docs/images/admin-agent-pools.png)
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

- **API-first** -- every UI action is backed by a public API endpoint
- **BFF pattern** -- Next.js frontend is the single ingress entry point; browser never talks to the API directly
- **Kubernetes-native** -- deployed exclusively via Helm chart; runner Jobs are ephemeral K8s Jobs
- **ARC-pattern execution** -- listener creates Jobs on demand (like GitHub Actions Runner Controller)
- **OpenTofu-first** -- [OpenTofu](https://opentofu.org/) is the recommended execution backend; `terraform` is also supported
- **Single organization** -- one org per instance (the literal name `default`); a deliberate fit for self-hosted, aligned with HashiCorp's own current guidance to consolidate onto a single org. Need separate tenants? Run an instance per tenant. See [Why a single organization](docs/architecture.md#why-a-single-organization)
- **Native object storage** -- speaks each cloud provider's native SDK (S3, Azure Blob, GCS) with filesystem fallback for dev

---

## Quick Start

Terrapod runs **only on Kubernetes** (the runner uses the Jobs API). Deploy it onto any cluster — or a single-node [k3s](https://k3s.io/) VM — with the Helm chart.

### Prerequisites

- A Kubernetes cluster (1.27+). No cluster? `curl -sfL https://get.k3s.io | sh -` gives you one on a single VM, with an ingress controller (Traefik) and storage included.
- Helm 3.x
- **External** PostgreSQL 14+ and Redis 7+ (the chart does not bundle them) — a managed service or run them on the cluster/VM.

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

- **Local passwords** -- PBKDF2-SHA256 hashed, with zxcvbn strength validation
- **OIDC** -- Auth0, Okta, Azure AD, and any standards-compliant provider via authlib
- **SAML** -- Azure AD SAML and other SAML 2.0 providers via python3-saml
- **terraform login** -- OAuth2 Authorization Code with PKCE for CLI authentication
- **API tokens** -- long-lived tokens for automation, SHA-256 hashed at rest

See [docs/authentication.md](docs/authentication.md) for setup guides.

---

## Documentation

| Document | Description |
|---|---|
| [Architecture](docs/architecture.md) | System components, BFF pattern, storage, runners, auth flows |
| [Getting Started](docs/getting-started.md) | Deploy the Helm chart on Kubernetes (or k3s), first workspace, first plan/apply |
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
| [Notifications](docs/notifications.md) | Webhook, Slack, and email alerts on run events |
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
| [Production Checklist](docs/production-checklist.md) | Pre-go-live checklist for a production deployment |
| [Disaster Recovery](docs/disaster-recovery.md) | Break-glass state recovery, shipped DB backup CronJob + restore-verification DR drill, per-backend object-storage protection |
| [Encryption at Rest](docs/encryption-at-rest.md) | Optional off-by-default app-layer (BYOK) envelope encryption of DB secrets **and state files** — for no-/niche-CSP, bare-metal, or air-gapped deployments (static / Vault Transit / AWS KMS) |

---

## Tech Stack

| Layer | Technology |
|---|---|
| API server | Python 3.13+ / FastAPI / SQLAlchemy (async) / Pydantic |
| Database | PostgreSQL |
| Cache / Sessions | Redis |
| Object storage | AWS S3, Azure Blob, GCS, or filesystem (native SDKs) |
| Frontend | Next.js 16 / React 19 / TypeScript / Tailwind CSS / Radix UI |
| Runner listener | Python (same codebase as API) |
| Auth | authlib (OIDC), python3-saml (SAML) |
| Deployment | Helm chart on Kubernetes |
| CI | GitHub Actions |

---

## Development

All builds, tests, and linting run in Docker -- no local Python or Node.js install needed.

```zsh
make dev          # Start local dev environment (Tilt)
make dev-down     # Stop local dev environment
make test         # Run pytest in Docker (with LocalStack for S3)
make lint         # Run ruff + mypy in Docker
make images       # Build production Docker images
```

### Conventions

- **Issue-first**: every change beyond a trivial tweak starts with a GitHub issue; the PR references it (`closes #N`)
- **Commits**: conventional commits (`feat:`, `fix:`, `docs:`, `chore:`)
- **Branches**: feature branches off `main`; never push directly to `main`
- **API contract**: JSON:API spec; compatibility tested against `go-tfe` client
- **Migrations**: Alembic with async SQLAlchemy
- **Local dev**: Tilt with live_update for Python and Node.js hot reload

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
|-------|------|----------------|
| SAST | [Semgrep](https://semgrep.dev/) | OWASP Top 10, secrets detection, project-specific rules (naive datetimes, raw background tasks) |
| Container scanning | [Trivy](https://trivy.dev/) | HIGH/CRITICAL CVEs in `terrapod-api` and `terrapod-web` images |
| DAST | [Nuclei](https://nuclei.projectdiscovery.io/) | Auth bypass, header injection, CORS validation, state endpoint security, HTTP method restriction |

Reports are written to `reports/pentest/`. See [SECURITY.md](SECURITY.md) for the full security policy.

---

## Comparison with Alternatives

| Project | What it does | Position relative to Terrapod |
|---|---|---|
| [Terrakube](https://terrakube.io/) | Open-source TFC/TFE replacement | **Closest peer** -- comparable full-platform scope (see below) |
| [OpenTofu](https://opentofu.org/) | Open-source Terraform fork (CLI) | CLI only -- no collaboration platform; Terrapod runs it as an engine |
| [Atlantis](https://www.runatlantis.io/) | PR-based plan/apply automation | No UI, no state management, no registry, no RBAC |
| [Digger](https://digger.dev/) | CI-native Terraform orchestration | Runs inside CI; no standalone platform |
| [Terrateam](https://terrateam.io/) | GitHub-integrated TF automation | GitHub-coupled; limited community edition |
| [Spacelift](https://spacelift.io/) | Commercial TF management platform | Not open source |

### Terrakube

[Terrakube](https://terrakube.io/) is the closest open-source alternative and the project most worth comparing against. It is **also** a full self-hosted Terraform Cloud / Enterprise replacement: it implements the same `cloud {}` / `backend "remote"` TFE V2 API that Terrapod targets, and ships organizations, a private module + provider registry with GPG-signed provider publishing, granular RBAC, VCS integration (GitHub/GitLab/Bitbucket/Azure DevOps), dynamic provider credentials (AWS/GCP/Azure workload identity), OPA policy checks, and ephemeral Kubernetes-Job executors. It is Apache-2.0, built on Java/Spring Boot + Angular, with an established community and a frequent release cadence. **If you are choosing a Terraform platform today, evaluate Terrakube alongside Terrapod** -- on the core surface the two are at rough parity, and Terrakube is the more mature project.

**Where Terrakube differs from Terrapod:**

- **Multi-organization tenancy** with teams. Terrapod is single-org by deliberate design — a choice aligned with [HashiCorp's own current guidance](https://developer.hashicorp.com/validated-patterns/terraform/migrate-terraform-orgs-projects), which now recommends *minimizing* organizations and consolidating onto one (segmenting internally instead). Terrapod's tenant boundary is the deployment: for separate tenants, run an instance per tenant; for segmentation within one company, label-based RBAC covers what projects/teams do. If you specifically need several named organizations behind a single endpoint, Terrakube offers that and Terrapod does not — see [Why a single organization](docs/architecture.md#why-a-single-organization).

**Where Terrakube is more mature:**

- **Maturity**: longer track record, larger community, more permissive (Apache-2.0) license. Terrapod is newer and backed by a small core team.

**Where Terrapod is genuinely differentiated** (verified against Terrakube's current docs). The first three share one theme -- Terrapod is built for restricted-network, multi-cluster, low-upstream-dependency topologies:

- **Firewall-friendly cross-cluster execution.** Terrapod runners connect *outbound* to the control plane over SSE and create Jobs locally; the API holds no inbound reach and no Kubernetes access into the execution cluster. Terrakube's API connects *into* the executor (it must be exposed via ingress, with Redis reachable), so isolated / NAT'd / outbound-only execution clusters aren't supported the same way.
- **Polling-first VCS** -- Terrapod supports inbound webhooks (GitHub and GitLab) but does not require them: it also polls VCS over outbound HTTPS, so the integration works behind firewalls/NATs with no inbound delivery. Terrakube uses webhook delivery. Different fits for inbound-restricted networks.
- **Pull-through provider mirror + terraform/tofu binary cache** -- runners have zero direct upstream dependency; Terrakube ships a local plugin cache.
- **Monorepo autodiscovery** -- Atlantis-style auto-creation of workspaces from glob-matched directories on PRs (Terrakube has directory filtering, but not auto-creation).
- **Run tasks** -- pre/post-plan external webhook validation hooks (not present in Terrakube).
- **In-platform AI** -- plan summaries, failure analysis, and chat (Terrakube integrates AI via an external MCP server).
- **Native Terragrunt** -- a per-workspace flag wraps agent-mode runs in `terragrunt` (pull-through binary cache, local-backend reconciliation) while Terrapod keeps owning state and the run lifecycle; CLI-driven runs need no config. Something TFE/HCP Terraform never did. See [docs/terragrunt.md](docs/terragrunt.md).
- Additionally: first-class OPA **policy sets** with mandatory/advisory enforcement, native multi-channel **notifications** (Slack/email/webhook), and cross-workspace **run triggers**.

Net: Terrapod is not a "better general TFE replacement" -- Terrakube is the more mature project and offers multi-org tenancy for those who want it (Terrapod is deliberately single-org, in line with [HashiCorp's current direction](https://developer.hashicorp.com/validated-patterns/terraform/migrate-terraform-orgs-projects)). Terrapod's defensible niche is **restricted-network, multi-cluster execution** (outbound-only runners, polling VCS, self-contained caching) with an AI-assisted review layer. Pick on that basis.

Licensing: Terrapod is **GPLv3** (strong copyleft); Terrakube is **Apache-2.0** (permissive) -- relevant if you intend to redistribute a modified platform.

---

## License

[GPLv3](LICENSE) -- strong copyleft ensures Terrapod and all derivative works remain open source.

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
