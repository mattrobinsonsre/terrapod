# Terrapod

**Open-source platform replacement for Terraform Enterprise.**

Terrapod provides the collaboration, governance, state management, and UI layer that wraps around `terraform` or `tofu` as pluggable execution backends. It is compatible with the `terraform`/`tofu` **`cloud` backend**: it implements the subset of the [HCP Terraform / TFE V2 API](https://developer.hashicorp.com/terraform/enterprise/api-docs) those CLIs consume (over the [`go-tfe`](https://pkg.go.dev/github.com/hashicorp/go-tfe) protocol) as a stable contract at `/api/v2/`, so existing `cloud` blocks and CI/CD point at a Terrapod instance with minimal reconfiguration. Everything else -- workspace/registry/RBAC management, agent pools, and the rest -- is Terrapod's own API at `/api/terrapod/v1/`; Terrapod does not reimplement the full TFE V2 API, and is not a general drop-in for `go-tfe`-based automation or the `hashicorp/tfe` provider.

Terrapod is **not** a fork of Terraform or OpenTofu. It orchestrates them.

![Workspaces](images/workspaces.png)

---

## Why Terrapod

Beyond broad TFE compatibility, Terrapod is built with three deliberate design foci:

- **Restricted-network & multi-cluster execution.** Runner listeners connect *outbound* over SSE and create Kubernetes Jobs locally, so the API never needs inbound reach into the clusters where runs happen. VCS is polling-first (webhooks optional), and a pull-through provider mirror + CLI binary cache — with an air-gap **sealed mode** — let runners resolve providers and binaries with no upstream internet for cached platforms. See [Split-networking deployments](deployment-network-isolation.md) and the [ARC execution model](architecture.md#runner-architecture-arc-pattern).
- **An AI-augmented review layer.** Every plan can carry an LLM-generated change summary and risk assessment, with failure analysis on errored plans and a chat to interrogate a run — provider-agnostic via LiteLLM, and **disabled by default**. See [AI Plan Summary](ai-plan-summary.md).
- **A low contribution barrier.** The platform core is **Python** (FastAPI + async SQLAlchemy); the consumer ecosystem (Go SDK, Terraform provider, migration/publish CLIs) is **Go**. AI-assisted contributions are welcome — see [`llms.txt`](../llms.txt), [AGENTS.md](../AGENTS.md), and [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Features

| Feature | Description |
|---|---|
| **Workspaces** | Isolate state, variables, and runs per workspace |
| **Remote State Management** | Versioned state storage with locking, rollback, encryption at rest via CSP services |
| **Agent Execution** | Plan/apply runs on the server via K8s Job-based runner infrastructure |
| **VCS Integration** | GitHub (App) and GitLab (access token); inbound webhooks supported (GitHub HMAC + GitLab token) for instant triggers, plus outbound polling so webhooks are optional, never required |
| **VCS Workflows** | Default merge-then-apply (TFE standard) plus opt-in apply-then-merge mode (Atlantis-style: PR comments drive applies, `terrapod apply` from a PR comment, auto-merge after apply) |
| **Variables & Secrets** | Per-workspace env and Terraform variables; sensitive values protected by database encryption-at-rest; variable sets |
| **RBAC** | Label-based role system with hierarchical workspace permissions (read/plan/write/admin) |
| **Private Registry** | Publish, version, and share modules and providers internally with pull-through caching |
| **Service Catalog** | No-code self-service provisioning over the private registry; blessed modules become one-click agent-mode workspaces with provider templates and a dedicated RBAC axis |
| **Agent Pools** | Named groups of runner listeners; join token → certificate exchange for auth |
| **SSO (OIDC / SAML)** | Pluggable identity providers (Auth0, Okta, Azure AD, etc.) |
| **Run Triggers** | Cross-workspace dependency chains -- source apply triggers downstream runs |
| **Stale-plan Guards** | Auto-discard a plan that no longer reflects reality: state-version drift (always on) + optional per-workspace time-based plan expiry |
| **Audit Logging** | Immutable event log with configurable retention |
| **Notifications** | Webhook (HMAC-SHA512), Slack (Block Kit), and email alerts on run events |
| **Interactive Slack app** | Outbound Socket Mode app: `/terrapod` account linking (explicit confirm step) + opt-in per-workspace run notifications with RBAC-checked Approve/Discard buttons; multiple deployments can share one Slack workspace via per-deployment `slack.command`/`slack.label` ([Slack integration](slack-integration.md)) |
| **Run Tasks** | Pre/post-plan webhook hooks for external validation |
| **Execution Hooks** | **Custom execution steps** — admin-managed shell run in the runner Job at five run-lifecycle points, associated with workspaces (`pre_init` is the setup/tooling/auth slot; custom runner images cover heavier needs) |
| **Policy-as-Code** | OPA/Rego policy sets evaluated on every run; advisory or mandatory enforcement, label-scoped |
| **Drift Detection** | Scheduled plan-only runs to detect out-of-band infrastructure changes |
| **Workspace Health** | Per-workspace health conditions with status indicators on workspace list |
| **Cloud Credentials** | Dynamic provider credentials via Kubernetes workload identity (AWS IRSA, GCP WIF, Azure WI) |
| **Binary Caching** | Pull-through cache for terraform/tofu/terragrunt CLI binaries; download base + version-index sources are operator-overridable to an internal mirror (restricted-network / air-gapped) and honour the forward proxy/CA |
| **Supply-chain Verification** | Cached binaries + provider archives verified against the publisher's GPG-signed SHA256SUMS with pinned keys; the runner re-verifies the executable (visible in the run log) before running it |
| **Terragrunt** | Per-workspace Terragrunt for agent-mode runs (flag + version, pull-through binary cache, local-backend reconciliation); CLI-driven runs work with zero config |
| **Workspace Autodiscovery** | Atlantis-style monorepo autodiscovery with rule templating; safe-by-default rename/delete/orphan lifecycle (opt-in destroy) |
| **Bulk Workspace Operations** | Server-side workspace search + all-or-nothing bulk settings update (dry-run by default; never triggers runs) |
| **Cross-Workspace Remote State** | `terraform_remote_state` composition with a producer-controlled consumer allowlist (secure by default; secret-bearing state stays with its owner) |
| **Migrate in (TFE / HCP / Atlantis)** | [`terrapod-migrate`](migration.md) — a dry-run-first, reversible CLI that moves an existing Terraform Enterprise / HCP Terraform / Atlantis platform onto Terrapod: previews, creates the core (VCS connections, workspaces, variables, variable sets, state with serial + lineage preserved, run triggers, notifications, agent pools, registry signing keys), verifies parity, and rolls back cleanly. Registry versions are reported for re-publish; RBAC is suggested, never auto-applied |

---

## Quick Start

**Just want to try it?** One command spins up a throwaway [kind](https://kind.sigs.k8s.io/) or [k3d](https://k3d.io/) cluster, batteries-included — chart-managed PostgreSQL + Redis, filesystem storage, a local admin — and prints the URL + login:

```zsh
make eval        # boots the stack; `make eval-down` tears it down
```

See the [README's Quick Evaluation section](https://github.com/mattrobinsonsre/terrapod#quick-evaluation) for details. The eval profile is for kicking the tyres only — not production.

For a real deployment onto any Kubernetes cluster (or a single-node [k3s](https://k3s.io/) VM) with the Helm chart:

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

For production, PostgreSQL and Redis are external (managed) services — the chart can run them in-cluster (`postgresql.deploy=true` / `redis.deploy=true`) but that single-replica, no-backup mode is for evaluation/dev only. See [Getting Started](getting-started.md) for the full walkthrough, [Deployment](deployment.md) for the datastore options, or [Local Development](local-development.md) if you want to run Terrapod from source.

---

## Architecture

```
Browser / CLI  ──▶  Ingress  ──▶  Next.js (BFF)  ──▶  FastAPI API
                                                          │
                            ┌──────────┬──────────┬───────┘
                            ▼          ▼          ▼
                       PostgreSQL    Redis    Object Storage
                                    ▲  ▲             ▲
                          SSE events│  │Job status    │
                             Runner Listener ─────────┘
                                    │
                              K8s Jobs (terraform/tofu)
```

- **API-first** -- every UI action is backed by a public API endpoint
- **BFF pattern** -- Next.js is the single ingress entry point; browser never talks to the API directly
- **Responsive, mobile-first web UI** -- every surface adapts from desktop tables to touch-friendly card layouts on phones (one viewport-driven implementation, no separate mobile app)
- **Kubernetes-native** -- deployed exclusively via Helm chart
- **ARC-pattern execution** -- listener receives events via SSE, creates ephemeral K8s Jobs; API reconciler owns all run state

See [Architecture](architecture.md) for the full breakdown.

---

## Documentation

| Guide | Description |
|---|---|
| [Alternatives to Terraform Enterprise / Terraform Cloud](alternatives.md) | Where Terrapod fits among open-source TACOS (Terrakube, Digger, …); is-there-a-free-alternative answers, a neutral comparison, and when to pick Terrapod |
| [FAQ](faq.md) | Straight answers to the common buyer questions (free? OpenTofu? Terragrunt? air-gapped? production-ready? vs Terrakube/Atlantis/Digger?) |
| [Getting Started](getting-started.md) | Deploy the Helm chart on Kubernetes (or k3s), first workspace, first plan/apply |
| [Migration](migration.md) | Move a TFE / HCP Terraform or Atlantis platform onto Terrapod with `terrapod-migrate` — dry-run-first, reversible, with an explicit "what transfers vs. what's a checklist" breakdown |
| [Local Development](local-development.md) | Run Terrapod from source with Tilt (contributors only) |
| [Architecture](architecture.md) | System components, storage, runners, auth flows |
| [Authentication](authentication.md) | Local auth, OIDC, SAML, terraform login, API tokens, scoped service tokens (bound/detached) + offboarding idle guard |
| [RBAC](rbac.md) | Permission model, label-based access control, custom roles |
| [VCS Integration](vcs-integration.md) | GitHub and GitLab setup, polling, webhooks |
| [VCS Workflows](vcs-workflows.md) | merge_then_apply (default) vs apply_then_merge (Atlantis-style, opt-in) |
| [Autodiscovery](autodiscovery.md) | Atlantis-style monorepo workspace autodiscovery |
| [Drift Detection](drift-detection.md) | Scheduled plan-only runs to detect infrastructure drift |
| [Drift Ignore Rules](drift-ignore-rules.md) | Per-workspace allowlist that suppresses known-noisy attributes from the drift signal (e.g. provider-rotated certs, externally co-managed replicas) |
| [Supply-chain Verification](supply-chain-verification.md) | How cached binaries/providers and runner executables are verified against publisher signatures (pinned keys, `verify` knobs, air-gap) |
| [Encryption at Rest](encryption-at-rest.md) | Optional, off-by-default app-layer (BYOK) envelope encryption of DB secrets **and state files** — for no-/niche-CSP / bare-metal / air-gapped deployments; belt-and-braces if your CSP already encrypts at rest. Providers: static / Vault Transit / AWS KMS |
| [Run Triggers](run-triggers.md) | Cross-workspace dependency chains |
| [Terragrunt](terragrunt.md) | CLI-driven and agent-mode Terragrunt support, the agent-mode `terragrunt_enabled` flag, and current limitations |
| [Remote State](remote-state.md) | Cross-workspace `terraform_remote_state` composition with producer-controlled allowlist |
| [AI Plan Summary](ai-plan-summary.md) | LLM-generated change summary + risk assessment on every plan; failure analysis on errored plans. Bedrock, OpenAI, Anthropic, Gemini, vLLM — any provider via LiteLLM |
| [Impact Graph](impact-graph.md) | Interactive dependency + blast-radius view of a plan on the run page, clustered by module; click a resource to light up its transitive downstream impact |
| [Estate Topology](estate-topology.md) | Whole-estate dependency + module-impact graph — workspaces + modules wired by run-triggers, remote-state, and module links; group by any label / pool / name prefix; RBAC-filtered; accessible table fallback |
| [State Resource Graph](state-resource-graph.md) | Per-workspace resource dependency graph from Terraform state — resources wired by `depends-on`; current state version by default with an older-version picker; group by type / module / provider / mode; accessible table fallback |
| [Notifications](notifications.md) | Webhook, Slack, and email alerts on run events |
| [Run Tasks](run-tasks.md) | Pre/post-plan webhook hooks for external validation |
| [Execution Hooks](execution-hooks.md) | Custom shell steps in the runner Job at five lifecycle points |
| [Policy-as-Code](policies.md) | OPA/Rego policy sets, advisory/mandatory enforcement, label scoping |
| [Audit Logging](audit-logging.md) | Immutable event log, query API, retention |
| [Artifact Retention](artifact-retention.md) | Automated cleanup of old state versions, run logs, cache entries |
| [Runners](runners.md) | Custom runner images, private registries, Job configuration |
| [Cloud Credentials](cloud-credentials.md) | Zero static cloud credentials, end to end — runs and the platform reach cloud APIs via Kubernetes workload identity (AWS IRSA, GCP WIF, Azure WI). Beginner primer, decision tree, a preflight doctor (`make preflight-identity` / opt-in Helm hook) that verifies SA→role + object-store access before the first run, troubleshooting, passwordless **database** + **Redis/Valkey** IAM auth (AWS/GCP/Azure), and Vault/ESO patterns |
| [Registry](registry.md) | Private module/provider registry, caching layers |
| [Registry Publishing](registry-publishing.md) | Publishing providers/modules with the `terrapod-publish` CLI and the client-signed publish protocol |
| [Service Catalog](service-catalog.md) | No-code self-service provisioning over the private module registry: blessed catalog items, provider templates, a `catalog_permission` RBAC axis, and a full provision → reconfigure → destroy lifecycle |
| [Monitoring](monitoring.md) | Prometheus metrics, scraping, shipped Grafana dashboard + alert rules (with per-alert runbooks) |
| [Deployment](deployment.md) | Production Helm deployment, storage backends, scaling |
| [Split-networking deployments](deployment-network-isolation.md) | Three-Ingress model: management / webhook / internal agent path, with split-hostname runner config |
| [Optional split webhook ingress](deployment-webhook-ingress.md) | Optional second Ingress for the public-must-reach surface (VCS webhooks, run-task callbacks) |
| [Forward proxy & custom CA trust](deployment-proxy.md) | Route all outbound HTTP(S) through a corporate proxy and trust a private/MITM CA, across every component including runner Jobs |
| [Security Hardening](security-hardening.md) | TLS, secrets management, network policies, rate limiting |
| [Versioning & Support](versioning-and-support.md) | What each version bump guarantees, the stable surfaces + their CI gates, component version-skew support, the deprecation window, and the support matrix |
| [Deprecations](deprecations.md) | The authoritative list of deprecated surfaces and their sunset dates, plus how to read the API's `Deprecation`/`Sunset` headers |
| [Known Limitations](known-limitations.md) | What Terrapod does not (yet) do — deployment, scope, and feature constraints, stated plainly |
| [Production Checklist](production-checklist.md) | Step-by-step checklist for go-live readiness |
| [Disaster Recovery](disaster-recovery.md) | Break-glass state recovery, shipped DB backup CronJob + restore-verification DR drill, per-backend object-storage protection |
| [API Reference](api-reference.md) | All API endpoints with examples |

---

## License

[MPL-2.0](https://github.com/mattrobinsonsre/terrapod/blob/main/LICENSE) -- file-level copyleft keeps Terrapod's own source open while staying friendly to enterprise adoption (the same license as OpenTofu and the historical Terraform codebase).

---

## Trademarks

Terrapod is not affiliated with, endorsed by, or a product of HashiCorp, Inc. or IBM. Terraform is a trademark of HashiCorp, Inc. OpenTofu is a project of the Linux Foundation.
