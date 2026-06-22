# Terrapod

[![CI](https://github.com/mattrobinsonsre/terrapod/actions/workflows/ci.yml/badge.svg)](https://github.com/mattrobinsonsre/terrapod/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

**Open-source platform replacement for Terraform Enterprise.**

Terrapod provides the collaboration, governance, state management, and UI layer that wraps around `terraform` or `tofu` as pluggable execution backends. It targets API compatibility with the [HCP Terraform / TFE V2 API](https://developer.hashicorp.com/terraform/enterprise/api-docs) so that existing tooling -- the `terraform` CLI with `cloud` block, the [`go-tfe`](https://pkg.go.dev/github.com/hashicorp/go-tfe) client, CI/CD integrations -- can point at a Terrapod instance with minimal reconfiguration.

Terrapod is **not** a fork of Terraform or OpenTofu. It orchestrates them.

![Workspaces](docs/images/workspaces.png)

> **Drop-in replacement for HCP Terraform.** Point your existing `cloud` blocks, `go-tfe` clients, and CI/CD pipelines at Terrapod — zero code changes required.

> **AI-augmented plans.** Every plan can carry an LLM-generated change description, risk assessment, and (on failure) suggested fixes — provider-agnostic via [LiteLLM](https://github.com/BerriAI/litellm). Wire AWS Bedrock (Claude, Nova, gpt-oss) with native IAM auth, or point at OpenAI, Anthropic, Gemini, Azure OpenAI, or any OpenAI-compatible endpoint. See [docs/ai-plan-summary.md](docs/ai-plan-summary.md).

> **Policy-as-code with OPA.** Block applies on policy violations using [Open Policy Agent](https://www.openpolicyagent.org/) and the Rego language — the open-source equivalent of TFE's proprietary Sentinel. Policy sets are scoped to workspaces with the same label-based model as roles, evaluated on the runner against the plan JSON, and gated as `advisory` (warn) or `mandatory` (block). See [docs/policies.md](docs/policies.md).

---

## Key Features

| Feature | Status | Description |
|---|---|---|
| Workspaces | Implemented | Isolate state, variables, and runs per workspace |
| Remote State Management | Implemented | Versioned state storage with locking, rollback, encryption at rest via CSP services |
| Agent Execution | Implemented | Plan/apply runs on the server via K8s Job-based runner infrastructure |
| VCS Integration | Implemented | GitHub (App) and GitLab (access token); polling-first with optional webhooks |
| Variables & Secrets | Implemented | Per-workspace env and Terraform variables; sensitive values protected by database encryption-at-rest; variable sets |
| RBAC | Implemented | Label-based role system with hierarchical workspace permissions (read/plan/write/admin) |
| Private Module Registry | Implemented | Publish, version, and share modules internally |
| Private Provider Registry | Implemented | Publish, version, and share providers with GPG signing and network mirror caching |
| Binary Caching | Implemented | Pull-through cache for terraform/tofu CLI binaries |
| Agent Pools | Implemented | Named groups of runner listeners; join token → certificate exchange for auth |
| CLI-Driven Runs | Implemented | `terraform plan` / `apply` via cloud backend (both `terraform` and `tofu` verified) |
| TFE V2 API | Implemented | JSON:API surface compatible with `go-tfe` / `terraform login` |
| Audit Logging | Implemented | Immutable event log with configurable retention |
| SSO (OIDC / SAML) | Implemented | Pluggable identity providers (Auth0, Okta, Azure AD, etc.) |
| Drift Detection | Implemented | Scheduled plan-only runs to detect out-of-band changes |
| Run Triggers | Implemented | Cross-workspace dependency chains — source apply triggers downstream runs |
| **AI Plan Summary** | **Implemented** | **LLM-generated change summary + risk assessment on every plan; failure analysis on errored plans. Provider-agnostic via LiteLLM — AWS Bedrock (Claude, Nova, gpt-oss…), OpenAI, Anthropic direct, Google Gemini, Azure OpenAI, vLLM. IAM-native auth for Bedrock (IRSA + optional cross-account `sts:AssumeRole`).** |
| **Policy-as-Code (OPA)** | **Implemented** | **Rego-based policy enforcement on plan output — the open-source equivalent of Sentinel. Advisory or mandatory sets, label-scoped to workspaces, evaluated on the runner against plan JSON, with admin-override on mandatory blocks. Author Rego, attach to workspaces by label, see pass/fail per policy on every run.** |
| Notifications | Implemented | Webhook (HMAC-SHA512), Slack (Block Kit), and email alerts on run events |
| Run Tasks | Implemented | Pre/post-plan webhook hooks for external validation |
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
- **Single organization** -- one org per instance; every Terrapod API path that contains an organization segment uses the literal name `default`
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
| [Policies (OPA)](docs/policies.md) | Rego policy authoring, advisory vs mandatory enforcement, label-based scoping, admin override |
| [Autodiscovery](docs/autodiscovery.md) | Atlantis-style monorepo workspace autodiscovery |
| [Drift Detection](docs/drift-detection.md) | Scheduled plan-only runs to detect infrastructure drift |
| [Run Triggers](docs/run-triggers.md) | Cross-workspace dependency chains |
| [Notifications](docs/notifications.md) | Webhook, Slack, and email alerts on run events |
| [Run Tasks](docs/run-tasks.md) | Pre/post-plan webhook hooks for external validation |
| [Audit Logging](docs/audit-logging.md) | Immutable event log, query API, retention |
| [Cloud Credentials](docs/cloud-credentials.md) | AWS IRSA, GCP WIF, Azure WI setup |
| [Monitoring](docs/monitoring.md) | Prometheus metrics, scraping, recommended alerts |
| [Disaster Recovery](docs/disaster-recovery.md) | Break-glass state recovery from object storage |

---

## Tech Stack

| Layer | Technology |
|---|---|
| API server | Python 3.13+ / FastAPI / SQLAlchemy (async) / Pydantic |
| Database | PostgreSQL |
| Cache / Sessions | Redis |
| Object storage | AWS S3, Azure Blob, GCS, or filesystem (native SDKs) |
| Frontend | Next.js 15 / React 19 / TypeScript / Tailwind CSS / Radix UI |
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

- **Commits**: conventional commits (`feat:`, `fix:`, `docs:`, `chore:`)
- **Branches**: feature branches off `main`; never push directly to `main`
- **API contract**: JSON:API spec; compatibility tested against `go-tfe` client
- **Migrations**: Alembic with async SQLAlchemy
- **Local dev**: Tilt with live_update for Python and Node.js hot reload

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

**Where Terrakube is ahead of Terrapod:**

- **Multi-organization tenancy** with teams -- Terrapod is single-org by deliberate design.
- **Maturity**: longer track record, larger community, more permissive (Apache-2.0) license. Terrapod is newer and, today, single-maintainer.

**Where Terrapod is genuinely differentiated** (verified against Terrakube's current docs). The first three share one theme -- Terrapod is built for restricted-network, multi-cluster, low-upstream-dependency topologies:

- **Firewall-friendly cross-cluster execution.** Terrapod runners connect *outbound* to the control plane over SSE and create Jobs locally; the API holds no inbound reach and no Kubernetes access into the execution cluster. Terrakube's API connects *into* the executor (it must be exposed via ingress, with Redis reachable), so isolated / NAT'd / outbound-only execution clusters aren't supported the same way.
- **Polling-first VCS** -- Terrapod polls VCS over outbound HTTPS (webhooks optional); Terrakube is webhook-only and needs inbound webhook delivery.
- **Pull-through provider mirror + terraform/tofu binary cache** -- runners have zero direct upstream dependency; Terrakube ships only a local plugin cache.
- **Monorepo autodiscovery** -- Atlantis-style auto-creation of workspaces from glob-matched directories on PRs (Terrakube has directory filtering, but not auto-creation).
- **Run tasks** -- pre/post-plan external webhook validation hooks (not present in Terrakube).
- **In-platform AI** -- plan summaries, failure analysis, and chat (Terrakube offers only an external MCP server).
- Additionally: first-class OPA **policy sets** with mandatory/advisory enforcement, native multi-channel **notifications** (Slack/email/webhook), and cross-workspace **run triggers**.

Net: Terrapod is not a "better general TFE replacement" -- Terrakube wins on maturity and multi-tenancy. Terrapod's defensible niche is **restricted-network, multi-cluster execution** (outbound-only runners, polling VCS, self-contained caching) with an AI-assisted review layer. Pick on that basis.

Licensing: Terrapod is **GPLv3** (strong copyleft); Terrakube is **Apache-2.0** (permissive) -- relevant if you intend to redistribute a modified platform.

---

## License

[GPLv3](LICENSE) -- strong copyleft ensures Terrapod and all derivative works remain open source.

---

## Trademarks

Terrapod is not affiliated with, endorsed by, or a product of HashiCorp, Inc. or IBM. Terraform is a trademark of HashiCorp, Inc. OpenTofu is a project of the Linux Foundation.

---

## Contributing

Contributions are welcome. Please follow these guidelines:

1. Fork the repository and create a feature branch from `main`
2. Follow conventional commit format (`feat:`, `fix:`, `docs:`, `chore:`)
3. Run tests (`make test`) and linting (`make lint`) before submitting
4. Ensure all CI checks pass
5. Open a pull request with a clear description of the change

For architecture questions or major changes, open an issue first to discuss the approach.
