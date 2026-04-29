# Production Readiness Checklist

A step-by-step checklist for preparing a Terrapod instance for production use. Each item links to the relevant guide for detailed instructions.

> **This checklist assumes a working Terrapod installation.** If you haven't deployed yet, start with the [Deployment Guide](deployment.md).

---

## Infrastructure

- [ ] **PostgreSQL is externally managed** -- RDS, Cloud SQL, Azure Database, or equivalent with automated backups and point-in-time recovery (PITR). Do not run PostgreSQL inside the Kubernetes cluster for production workloads. See [Deployment: Database](deployment.md#database).
- [ ] **Redis is externally managed** -- ElastiCache, MemoryDB, Azure Cache, or equivalent. Redis holds ephemeral data (sessions, scheduler state, listener heartbeats) so durability is not required, but availability is. See [Deployment: Redis](deployment.md#redis).
- [ ] **Object storage is configured** -- S3, Azure Blob, or GCS with encryption at rest enabled. Filesystem storage (PVC) is acceptable for single-replica deployments but does not support multi-replica or cross-AZ redundancy. See [Deployment: Storage Backends](deployment.md#storage-backends).
- [ ] **API pod ephemeral storage is provisioned** -- `api.ephemeralStorage.enabled: true` (default) gives each api pod replica its own PVC for streaming VCS tarballs. The configured `storageClass` MUST have `Delete` reclaim policy, otherwise PVs accumulate as orphans on every pod restart. See [Deployment: VCS archive streaming and ephemeral storage](deployment.md#vcs-archive-streaming-and-ephemeral-storage-required-for-monorepo-workspaces).
  - **AWS EKS**: use `xfs` or `gp3` (NOT `xfs-retain` / `gp3-retain`).
  - **k3s (single-node)**: built-in `local-path` works out of the box.
  - **k3s (multi-node)**: install a CSI provisioner (`longhorn`, `openebs-hostpath`) and set the storage class explicitly.
- [ ] **TLS is enforced end-to-end** -- Ingress terminates TLS with a valid certificate, database uses `sslmode=verify-full`, Redis uses `rediss://`. See [Security Hardening: TLS](security-hardening.md#tls-configuration).
- [ ] **DNS record points to the ingress** -- The public hostname resolves to the ingress load balancer.

---

## Authentication & Access Control

- [ ] **SSO provider is configured** -- At least one OIDC or SAML provider for production identity management. See [Authentication](authentication.md).
- [ ] **Local auth is disabled** (if SSO is the sole provider) -- Set `auth.local_enabled: false` to prevent password-based login. See [Security Hardening: Authentication](security-hardening.md#authentication-hardening).
- [ ] **API token TTL is appropriate** -- Review `auth.api_token_max_ttl_hours` (default: 8760 = 1 year). Shorter TTLs reduce blast radius of leaked tokens. See [Security Hardening: Authentication](security-hardening.md#authentication-hardening).
- [ ] **RBAC roles are defined** -- Custom roles with label-based allow/deny rules for workspace access. Review the built-in roles (`admin`, `audit`, `everyone`) and create project-specific roles. See [RBAC](rbac.md).
- [ ] **Admin accounts are minimised** -- Only operators who need full platform access should hold the `admin` role. Use `audit` for read-only compliance access.

---

## Secrets Management

- [ ] **No secrets in `values.yaml`** -- Database URLs, Redis URLs, SSO client secrets, and bootstrap passwords are all referenced via `existingSecret` / `secretKeyRef`. See [Security Hardening: Secrets](security-hardening.md#secrets-management).
- [ ] **External Secrets Operator** (recommended) -- Sync secrets from AWS Secrets Manager, Azure Key Vault, or GCP Secret Manager into Kubernetes Secrets automatically. See [Security Hardening: Secrets](security-hardening.md#secrets-management).
- [ ] **Bootstrap password is changed or bootstrap is disabled** -- After initial setup, either change the admin password or set `bootstrap.enabled: false` to prevent the bootstrap Job from running on upgrades.

---

## Networking & Isolation

- [ ] **Network policies are enabled** -- Terrapod ships four NetworkPolicies (API, web, listener, runner) that restrict traffic between components. Runners are explicitly denied access to PostgreSQL and Redis. Requires a CNI plugin (Calico, Cilium, Weave Net). See [Security Hardening: Network Policies](security-hardening.md#network-policies).
- [ ] **Rate limiting is enabled** -- Enabled by default (`rate_limit.enabled: true`). Review thresholds: 100 req/min general, 10 req/min auth endpoints. See [Security Hardening: Rate Limiting](security-hardening.md#rate-limiting).
- [ ] **Ingress routes only to the web frontend** -- The BFF pattern means the API is never directly exposed. Verify no additional ingress rules expose port 8000.

---

## Backups & Recovery

- [ ] **Database backups are automated** -- Managed databases (RDS, Cloud SQL) provide automated daily backups with PITR. Verify backup retention meets your compliance requirements. See [Security Hardening: Backup Strategy](security-hardening.md#backup-strategy).
- [ ] **Object storage versioning is enabled** -- S3 versioning, Azure Blob soft delete, or GCS versioning protects against accidental state file deletion. See [Security Hardening: Object Storage](security-hardening.md#object-storage-security).
- [ ] **Break-glass recovery procedure is tested** -- Follow the [Disaster Recovery](disaster-recovery.md) guide in a non-production environment to verify you can recover Terraform state directly from object storage if Terrapod is unavailable.
- [ ] **Kubernetes Secrets are backed up** -- OIDC client secrets, bootstrap credentials, and listener join tokens stored in K8s Secrets are not recoverable from the database. Back them up or ensure they can be regenerated from your secrets manager.

---

## Monitoring & Observability

- [ ] **Prometheus metrics are enabled** -- Set `metrics.enabled: true` and configure ServiceMonitor/PodMonitor scraping. See [Monitoring](monitoring.md).
- [ ] **Alerts are configured** -- At minimum, set up alerts for:
  - High API error rate (>5% 5xx over 5 minutes)
  - Stuck runs (no terminal transitions in 30 minutes)
  - Scheduler stalls (`run_reconciler` not executing in 5 minutes)
  - Storage errors (any in 5 minutes)
  - Database/Redis connection errors
  - See [Monitoring: Recommended Alerts](monitoring.md#recommended-alerts)
- [ ] **Structured logging is collected** -- Terrapod emits JSON-formatted logs to stdout. Configure your log aggregator (Loki, CloudWatch, Datadog) to ingest from all pods in the Terrapod namespace.
- [ ] **Health checks are verified** -- `GET /ready` returns 200 when database, Redis, and storage are all reachable. Configure your load balancer or ingress to use this endpoint.

---

## Scaling & Availability

- [ ] **API runs at least 2 replicas** -- PodDisruptionBudget (`maxUnavailable: 1`) is enabled by default. A single replica means downtime during rolling updates.
- [ ] **HPA is configured for the API** -- Recommended: min 2, max 10 replicas targeting 70% CPU. See [Deployment: Scaling](deployment.md#scaling).
- [ ] **Database connection pool is sized correctly** -- Total max connections = `(pool_size + max_overflow) x replicas`. Verify this does not exceed the database's `max_connections`. See [Deployment: Connection Pooling](deployment.md#connection-pool-tuning).
- [ ] **Pod anti-affinity spreads replicas across nodes** -- Prevent all API replicas from landing on the same node. The Helm chart supports `affinity` configuration for API, web, and listener Deployments.

---

## Runner Infrastructure

- [ ] **Agent pool is created** -- At least one agent pool exists for scheduling runs. See [Architecture: Agent Pools](architecture.md).
- [ ] **Listener is deployed and connected** -- The listener registers via join token, maintains heartbeat, and appears as "online" in the admin UI.
- [ ] **Runner resource limits are appropriate** -- Default: 1 CPU / 2Gi memory (requests), 2 CPU / 4Gi (limits). Adjust per-workspace via `resource_cpu` and `resource_memory` for workspaces managing large state files. See [Deployment: Runner Jobs](deployment.md#runner-jobs).
- [ ] **Cloud workload identity is configured** (if applicable) -- AWS IRSA, GCP WIF, or Azure WI annotations on the runner ServiceAccount. See [Cloud Credentials](cloud-credentials.md).
- [ ] **Graceful termination period is sufficient** -- Default 120 seconds. Increase for workspaces with large provider downloads or slow applies. See [Deployment: Graceful Termination](deployment.md#runner-jobs).

---

## Audit & Compliance

- [ ] **Audit log retention is set** -- Default: 90 days. Set `audit.retention_days` to meet your compliance requirements (SOC2/ISO27001 typically require 365 days). See [Audit Logging](audit-logging.md).
- [ ] **Audit log is queryable** -- Verify the `/admin/audit-log` page shows recent API activity and that filters work correctly.
- [ ] **State-diverged workspaces are monitored** -- A workspace flagged as `state_diverged` means a runner applied changes but failed to upload the resulting state. This requires immediate operator intervention. Monitor via the health dashboard or the `terrapod_runs_terminal_total{terminal_state="errored"}` metric.

---

## VCS Integration (if applicable)

- [ ] **VCS connection is configured** -- GitHub App or GitLab access token. See [VCS Integration](vcs-integration.md).
- [ ] **Webhook secret is set** (GitHub only) -- If using webhook-accelerated polling, set `vcs.github.webhook_secret` and configure the webhook in your GitHub App settings.
- [ ] **Poll interval is appropriate** -- Default: 60 seconds. Lower values detect changes faster but increase API calls to your VCS provider.

---

## Pre-Go-Live Validation

- [ ] **Create a test workspace** -- Verify the full lifecycle: create workspace, connect VCS (if applicable), queue a plan, review output, confirm apply, verify state is stored.
- [ ] **Verify `terraform login`** -- Run `terraform login <hostname>` from a developer machine and confirm the PKCE flow completes successfully.
- [ ] **Verify `terraform plan` / `terraform apply`** -- Run a plan/apply cycle against a non-production workspace using the `cloud` block.
- [ ] **Verify state locking** -- Run two concurrent plans against the same workspace and confirm the second is rejected with a lock conflict (409).
- [ ] **Simulate a failover** -- Kill an API pod and verify that requests continue to be served by remaining replicas without data loss.
- [ ] **Review the security hardening guide** -- Walk through the full [Security Hardening Guide](security-hardening.md) and verify each recommendation is addressed or explicitly accepted as a risk.
