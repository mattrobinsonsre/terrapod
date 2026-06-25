# Terrapod

**Open-source platform replacement for Terraform Enterprise.**

Terrapod provides the collaboration, governance, state management, and UI layer that wraps around `terraform` or `tofu` as pluggable execution backends. It targets API compatibility with the [HCP Terraform / TFE V2 API](https://developer.hashicorp.com/terraform/enterprise/api-docs) so that existing tooling -- the `terraform` CLI with `cloud` block, the [`go-tfe`](https://pkg.go.dev/github.com/hashicorp/go-tfe) client, CI/CD integrations -- can point at a Terrapod instance with minimal reconfiguration.

Terrapod is **not** a fork of Terraform or OpenTofu. It orchestrates them.

![Workspaces](images/workspaces.png)

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
| **Audit Logging** | Immutable event log with configurable retention |
| **Notifications** | Webhook (HMAC-SHA512), Slack (Block Kit), and email alerts on run events |
| **Run Tasks** | Pre/post-plan webhook hooks for external validation |
| **Policy-as-Code** | OPA/Rego policy sets evaluated on every run; advisory or mandatory enforcement, label-scoped |
| **Drift Detection** | Scheduled plan-only runs to detect out-of-band infrastructure changes |
| **Workspace Health** | Per-workspace health conditions with status indicators on workspace list |
| **Cloud Credentials** | Dynamic provider credentials via Kubernetes workload identity (AWS IRSA, GCP WIF, Azure WI) |
| **Binary Caching** | Pull-through cache for terraform/tofu/terragrunt CLI binaries |
| **Terragrunt** | Per-workspace Terragrunt for agent-mode runs (flag + version, pull-through binary cache, local-backend reconciliation); CLI-driven runs work with zero config |
| **Workspace Autodiscovery** | Atlantis-style monorepo autodiscovery with rule templating; safe-by-default rename/delete/orphan lifecycle (opt-in destroy) |
| **Bulk Workspace Operations** | Server-side workspace search + all-or-nothing bulk settings update (dry-run by default; never triggers runs) |
| **Cross-Workspace Remote State** | `terraform_remote_state` composition with a producer-controlled consumer allowlist (secure by default; secret-bearing state stays with its owner) |

---

## Quick Start

Deploy onto any Kubernetes cluster (or a single-node [k3s](https://k3s.io/) VM) with the Helm chart:

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

PostgreSQL and Redis are external (not bundled). See [Getting Started](getting-started.md) for the full walkthrough, or [Local Development](local-development.md) if you want to run Terrapod from source.

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
- **Kubernetes-native** -- deployed exclusively via Helm chart
- **ARC-pattern execution** -- listener receives events via SSE, creates ephemeral K8s Jobs; API reconciler owns all run state

See [Architecture](architecture.md) for the full breakdown.

---

## Documentation

| Guide | Description |
|---|---|
| [Getting Started](getting-started.md) | Deploy the Helm chart on Kubernetes (or k3s), first workspace, first plan/apply |
| [Local Development](local-development.md) | Run Terrapod from source with Tilt (contributors only) |
| [Architecture](architecture.md) | System components, storage, runners, auth flows |
| [Authentication](authentication.md) | Local auth, OIDC, SAML, terraform login, API tokens, scoped service tokens (bound/detached) + offboarding idle guard |
| [RBAC](rbac.md) | Permission model, label-based access control, custom roles |
| [VCS Integration](vcs-integration.md) | GitHub and GitLab setup, polling, webhooks |
| [VCS Workflows](vcs-workflows.md) | merge_then_apply (default) vs apply_then_merge (Atlantis-style, opt-in) |
| [Autodiscovery](autodiscovery.md) | Atlantis-style monorepo workspace autodiscovery |
| [Drift Detection](drift-detection.md) | Scheduled plan-only runs to detect infrastructure drift |
| [Drift Ignore Rules](drift-ignore-rules.md) | Per-workspace allowlist that suppresses known-noisy attributes from the drift signal (e.g. provider-rotated certs, externally co-managed replicas) |
| [Run Triggers](run-triggers.md) | Cross-workspace dependency chains |
| [Terragrunt](terragrunt.md) | CLI-driven and agent-mode Terragrunt support, the agent-mode `terragrunt_enabled` flag, and current limitations |
| [Remote State](remote-state.md) | Cross-workspace `terraform_remote_state` composition with producer-controlled allowlist |
| [AI Plan Summary](ai-plan-summary.md) | LLM-generated change summary + risk assessment on every plan; failure analysis on errored plans. Bedrock, OpenAI, Anthropic, Gemini, vLLM — any provider via LiteLLM |
| [Notifications](notifications.md) | Webhook, Slack, and email alerts on run events |
| [Run Tasks](run-tasks.md) | Pre/post-plan webhook hooks for external validation |
| [Policy-as-Code](policies.md) | OPA/Rego policy sets, advisory/mandatory enforcement, label scoping |
| [Audit Logging](audit-logging.md) | Immutable event log, query API, retention |
| [Artifact Retention](artifact-retention.md) | Automated cleanup of old state versions, run logs, cache entries |
| [Runners](runners.md) | Custom runner images, private registries, Job configuration |
| [Cloud Credentials](cloud-credentials.md) | Zero static cloud credentials, end to end — runs and the platform reach cloud APIs via Kubernetes workload identity (AWS IRSA, GCP WIF, Azure WI). Beginner primer, decision tree, troubleshooting, passwordless **database** + **Redis/Valkey** IAM auth (AWS/GCP/Azure), and Vault/ESO patterns |
| [Registry](registry.md) | Private module/provider registry, caching layers |
| [Registry Publishing](registry-publishing.md) | Publishing providers/modules with the `terrapod-publish` CLI and the client-signed publish protocol |
| [Service Catalog](service-catalog.md) | No-code self-service provisioning over the private module registry: blessed catalog items, provider templates, a `catalog_permission` RBAC axis, and a full provision → reconfigure → destroy lifecycle |
| [Monitoring](monitoring.md) | Prometheus metrics, scraping, recommended alerts |
| [Deployment](deployment.md) | Production Helm deployment, storage backends, scaling |
| [Split-networking deployments](deployment-network-isolation.md) | Three-Ingress model: management / webhook / internal agent path, with split-hostname runner config |
| [Optional split webhook ingress](deployment-webhook-ingress.md) | Optional second Ingress for the public-must-reach surface (VCS webhooks, run-task callbacks) |
| [Security Hardening](security-hardening.md) | TLS, secrets management, network policies, rate limiting |
| [Production Checklist](production-checklist.md) | Step-by-step checklist for go-live readiness |
| [Disaster Recovery](disaster-recovery.md) | Break-glass state recovery from object storage |
| [API Reference](api-reference.md) | All API endpoints with examples |

---

## License

[GPLv3](https://github.com/mattrobinsonsre/terrapod/blob/main/LICENSE) -- strong copyleft ensures Terrapod and all derivative works remain open source.

---

## Trademarks

Terrapod is not affiliated with, endorsed by, or a product of HashiCorp, Inc. or IBM. Terraform is a trademark of HashiCorp, Inc. OpenTofu is a project of the Linux Foundation.
