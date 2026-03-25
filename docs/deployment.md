# Production Deployment

Terrapod is a **self-hosted platform** -- each instance is deployed and operated by the end organisation on its own infrastructure. Terrapod is not a hosted service and does not transmit data to any external party.

Terrapod is deployed exclusively via Helm chart on Kubernetes. This guide covers production installation, configuration, storage backends, and operational considerations.

---

## Prerequisites

- Kubernetes 1.27+
- Helm 3.x
- PostgreSQL 14+ (external, managed)
- Redis 7+ (external, managed)
- Object storage (S3, Azure Blob, GCS) or PVC-backed filesystem
- TLS certificate for the ingress hostname
- DNS record pointing to the ingress

### Don't Have Kubernetes?

Terrapod requires Kubernetes because the runner infrastructure uses the Jobs API to schedule ephemeral plan/apply executions. However, you don't need a managed cloud cluster — [k3s](https://k3s.io/) runs a fully conformant Kubernetes distribution on a single VM with minimal overhead (~512MB RAM).

**What works identically on k3s:**

- All Terrapod features (workspaces, runs, state management, registry, VCS integration, RBAC, SSO, drift detection, notifications, run tasks)
- Runner Job scheduling and execution
- Helm chart installation (k3s includes Traefik ingress and local-path storage by default)
- Multi-replica API and listener deployments (on the same node)
- Filesystem storage backend with PVC

**What you lose without a managed cloud cluster:**

- **Horizontal Pod Autoscaling** — no additional nodes to scale onto (but you can still run multiple replicas within a single node's capacity)
- **Spot/preemptible instances** — single VM, no cost optimization via instance lifecycle
- **Multi-AZ redundancy** — single failure domain (mitigate with VM-level backups)
- **Cloud workload identity** — IRSA (AWS), Workload Identity Federation (GCP), and Azure Workload Identity require managed Kubernetes. Use static credentials or run cloud CLIs with environment variables instead

**Quick start (Ubuntu/Debian VM):**

```zsh
# Install k3s (single-node cluster)
curl -sfL https://get.k3s.io | sh -

# Verify
sudo k3s kubectl get nodes

# Install Helm
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# Set kubeconfig
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# Install Terrapod (filesystem storage, local PostgreSQL + Redis)
helm install terrapod oci://ghcr.io/mattrobinsonsre/terrapod \
  --namespace terrapod \
  --create-namespace \
  -f values-production.yaml
```

For production use on k3s, run PostgreSQL and Redis as external services (managed or on a separate VM) rather than in-cluster, so database state survives cluster recreation.

---

## Helm Chart Installation

Terrapod is published to GitHub Container Registry (GHCR) as both Docker images and an OCI Helm chart:

| Artifact | Registry |
|---|---|
| API image | `ghcr.io/mattrobinsonsre/terrapod-api` |
| Listener image | `ghcr.io/mattrobinsonsre/terrapod-listener` |
| Migrations image | `ghcr.io/mattrobinsonsre/terrapod-migrations` |
| Web UI image | `ghcr.io/mattrobinsonsre/terrapod-web` |
| Runner image | `ghcr.io/mattrobinsonsre/terrapod-runner` |
| Helm chart | `oci://ghcr.io/mattrobinsonsre/terrapod` |

### Basic Install

```zsh
helm install terrapod oci://ghcr.io/mattrobinsonsre/terrapod \
  --namespace terrapod \
  --create-namespace \
  --set ingress.enabled=true \
  --set ingress.hostname=terrapod.example.com \
  --set postgresql.url="postgresql+asyncpg://terrapod:PASSWORD@db.example.com:5432/terrapod" \
  --set redis.url="redis://redis.example.com:6379"
```

To install a specific version:

```zsh
helm install terrapod oci://ghcr.io/mattrobinsonsre/terrapod --version 0.1.2 \
  --namespace terrapod \
  --create-namespace
```

### Install with Values File

Create a `values-production.yaml`:

```yaml
api:
  replicas: 3
  config:
    log_level: info

    storage:
      backend: s3
      s3:
        bucket: terrapod-storage
        region: eu-west-1

    auth:
      local_enabled: true
      callback_base_url: "https://terrapod.example.com"
      session_ttl_hours: 12
      api_token_max_ttl_hours: 8760  # 1 year
      sso:
        default_provider: okta
        oidc:
          - name: okta
            display_name: "Okta SSO"
            issuer_url: "https://your-org.okta.com/oauth2/default"
            client_id: "your-client-id"
            scopes: ["openid", "profile", "email", "groups"]
            groups_claim: "groups"

    registry:
      enabled: true
      provider_cache:
        enabled: true
      binary_cache:
        enabled: true

    vcs:
      enabled: true
      poll_interval_seconds: 60

web:
  enabled: true
  replicas: 2

listener:
  enabled: true
  replicas: 1
  name: "production-listener"
  existingSecret: terrapod-listener-credentials  # K8s Secret with join_token key

ingress:
  enabled: true
  hostname: terrapod.example.com
  className: nginx
  tls: true
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod

runners:
  default: standard

postgresql:
  url: ""  # Injected via secret

redis:
  url: ""  # Injected via secret

bootstrap:
  adminEmail: admin@example.com
  existingSecret: terrapod-admin-credentials
```

```zsh
helm install terrapod oci://ghcr.io/mattrobinsonsre/terrapod \
  --namespace terrapod \
  --create-namespace \
  -f values-production.yaml
```

---

## Configuration Reference

The chart ships with a `values.schema.json` that validates all values at `helm install`/`helm lint` time. Unknown keys are rejected (`additionalProperties: false` on Terrapod-specific objects), so typos and stale values are caught before deployment.

### Global

| Value | Default | Description |
|---|---|---|
| `global.imagePullSecrets` | `[]` | Image pull secrets for private registries |
| `global.commonLabels` | `{}` | Extra labels applied to all resources |

### API Server

| Value | Default | Description |
|---|---|---|
| `api.replicas` | `1` | Number of API server replicas |
| `api.image.repository` | `ghcr.io/mattrobinsonsre/terrapod-api` | API Docker image |
| `api.image.tag` | `""` (appVersion) | Image tag |
| `api.resources.requests.cpu` | `250m` | CPU request |
| `api.resources.requests.memory` | `512Mi` | Memory request |
| `api.resources.limits.cpu` | `1` | CPU limit |
| `api.resources.limits.memory` | `1Gi` | Memory limit |
| `api.autoscaling.enabled` | `false` | Enable HPA (requires cloud storage backend) |
| `api.autoscaling.minReplicas` | `2` | HPA minimum replicas |
| `api.autoscaling.maxReplicas` | `10` | HPA maximum replicas |
| `api.autoscaling.targetCPUUtilizationPercentage` | `70` | HPA target CPU |
| `api.pdb.enabled` | `true` | Enable PodDisruptionBudget |
| `api.pdb.maxUnavailable` | `1` | PDB max unavailable (default) |
| `api.config.log_level` | `info` | Log level |
| `api.serviceAccount.create` | `true` | Create ServiceAccount |
| `api.serviceAccount.annotations` | `{}` | SA annotations (for cloud identity) |

### Web UI

| Value | Default | Description |
|---|---|---|
| `web.enabled` | `false` | Enable web UI deployment |
| `web.replicas` | `2` | Number of web replicas |
| `web.image.repository` | `ghcr.io/mattrobinsonsre/terrapod-web` | Web Docker image |
| `web.resources.requests.cpu` | `100m` | CPU request |
| `web.resources.requests.memory` | `256Mi` | Memory request |
| `web.autoscaling.enabled` | `false` | Enable HPA for web |
| `web.autoscaling.minReplicas` | `1` | HPA minimum replicas |
| `web.autoscaling.maxReplicas` | `5` | HPA maximum replicas |
| `web.autoscaling.targetCPUUtilizationPercentage` | `70` | HPA CPU target |
| `web.pdb.enabled` | `true` | Enable PodDisruptionBudget |
| `web.pdb.maxUnavailable` | `1` | PDB max unavailable (default) |

### Storage

| Value | Default | Description |
|---|---|---|
| `api.config.storage.backend` | `filesystem` | Storage backend: `s3`, `azure`, `gcs`, `filesystem` |
| `api.config.storage.s3.bucket` | `""` | S3 bucket name |
| `api.config.storage.s3.region` | `us-east-1` | AWS region |
| `api.config.storage.s3.prefix` | `""` | Key prefix |
| `api.config.storage.s3.endpoint_url` | `""` | Custom endpoint (LocalStack) |
| `api.config.storage.azure.account_name` | `""` | Azure storage account |
| `api.config.storage.azure.container_name` | `""` | Blob container |
| `api.config.storage.gcs.bucket` | `""` | GCS bucket name |
| `api.config.storage.gcs.project_id` | `""` | GCP project ID |
| `api.config.storage.filesystem.root_dir` | `/var/lib/terrapod/storage` | Filesystem root |
| `storage.filesystem.persistence.enabled` | `true` | Create PVC |
| `storage.filesystem.persistence.size` | `50Gi` | PVC size |
| `storage.filesystem.persistence.storageClass` | `""` | Storage class |

### Auth

| Value | Default | Description |
|---|---|---|
| `api.config.auth.local_enabled` | `true` | Enable local password auth |
| `api.config.auth.callback_base_url` | `""` | Externally-reachable URL for callbacks |
| `api.config.auth.session_ttl_hours` | `12` | Session lifetime |
| `api.config.auth.api_token_max_ttl_hours` | `168` | Max API token lifetime in hours (168 = 7 days) |
| `api.config.auth.sso.default_provider` | `""` | Default SSO provider name |
| `api.config.auth.sso.oidc` | `[]` | OIDC provider configurations |
| `api.config.auth.sso.saml` | `[]` | SAML provider configurations |

### Registry & Caching

| Value | Default | Description |
|---|---|---|
| `api.config.registry.enabled` | `true` | Enable private registry |
| `api.config.registry.provider_cache.enabled` | `true` | Enable provider caching |
| `api.config.registry.binary_cache.enabled` | `true` | Enable binary caching |

### VCS

| Value | Default | Description |
|---|---|---|
| `api.config.vcs.enabled` | `true` | Enable VCS integration |
| `api.config.vcs.poll_interval_seconds` | `60` | Poll interval |
| `api.config.vcs.github.existingSecret` | `""` | K8s Secret containing the GitHub webhook HMAC secret |
| `api.config.vcs.github.existingSecretKey` | `"webhook_secret"` | Key within the Secret |

### Drift Detection

| Value | Default | Description |
|---|---|---|
| `api.config.drift_detection.enabled` | `false` | Enable the drift detection scheduler |
| `api.config.drift_detection.poll_interval_seconds` | `300` | How often the scheduler checks for workspaces due for a drift scan |
| `api.config.drift_detection.min_workspace_interval_seconds` | `3600` | Minimum allowed per-workspace drift check interval (floor for `drift-detection-interval-seconds` on any workspace) |

Example configuration in `values-production.yaml`:

```yaml
api:
  config:
    drift_detection:
      enabled: true
      poll_interval_seconds: 300
      min_workspace_interval_seconds: 3600
```

When enabled, the drift detection scheduler runs as a periodic task (via the distributed scheduler) and creates plan-only runs with `-detailed-exitcode` for workspaces that have drift detection enabled and are past their check interval. The scheduler respects the per-workspace `drift-detection-interval-seconds` attribute, subject to the `min_workspace_interval_seconds` floor.

**Note:** Drift detection is automatically enabled on workspaces that have a VCS connection. Non-VCS workspaces default to drift detection disabled. This can be overridden per-workspace via the API or Terraform provider.

### Metrics

| Value | Default | Description |
|---|---|---|
| `api.config.metrics.enabled` | `true` | Expose `/metrics` endpoint and instrument HTTP requests |
| `api.config.metrics.serviceMonitor.enabled` | `false` | Create a Prometheus ServiceMonitor for the API (requires Prometheus Operator) |
| `api.config.metrics.serviceMonitor.interval` | `30s` | Scrape interval |
| `api.config.metrics.serviceMonitor.labels` | `{}` | Extra labels for Prometheus selector matching |
| `api.config.metrics.podMonitor.enabled` | `false` | Create a Prometheus PodMonitor for the listener (requires Prometheus Operator) |
| `api.config.metrics.podMonitor.interval` | `30s` | Scrape interval |
| `api.config.metrics.podMonitor.labels` | `{}` | Extra labels for Prometheus selector matching |

When `metrics.enabled` is true, the API server exposes:
- `terrapod_http_requests_total` — Counter (method, path_template, status)
- `terrapod_http_request_duration_seconds` — Histogram (method, path_template, status)
- Process metrics (CPU, memory, file descriptors)

The listener exposes on its health port (`8081`):
- `terrapod_listener_active_runs` — Gauge
- `terrapod_listener_identity_ready` — Gauge (1 or 0)
- `terrapod_listener_heartbeat_age_seconds` — Gauge

The ServiceMonitor and PodMonitor are **double-gated** — they require both `metrics.enabled: true` AND the respective monitor's `enabled: true`. This avoids CRD errors on clusters without the Prometheus Operator.

Example for a cluster with Prometheus Operator:

```yaml
api:
  config:
    metrics:
      enabled: true
      serviceMonitor:
        enabled: true
        labels:
          release: kube-prometheus-stack  # match your Prometheus instance selector
      podMonitor:
        enabled: true
        labels:
          release: kube-prometheus-stack
```

### Encryption at Rest

Terrapod delegates encryption at rest to the underlying infrastructure services. No application-level encryption key is required.

| Data | Storage | Encryption |
|---|---|---|
| Sensitive variables, VCS tokens | PostgreSQL | Database encryption-at-rest (RDS, Cloud SQL, Azure Database) |
| State files, config tarballs, logs | Object storage | Object store encryption-at-rest (S3 SSE, Azure Storage, GCS default) |

Enable encryption on your managed database and object storage services. For filesystem-backed storage, use encrypted volumes.

### Runner Listener

| Value | Default | Description |
|---|---|---|
| `listener.enabled` | `true` | Enable runner listener |
| `listener.image.repository` | `ghcr.io/mattrobinsonsre/terrapod-listener` | Listener Docker image |
| `listener.image.tag` | `""` (appVersion) | Image tag |
| `listener.replicas` | `1` | Number of listener replicas |
| `listener.name` | `"listener"` | Listener name (registered in the pool) |
| `listener.joinToken` | `""` | Raw join token (use `existingSecret` for production) |
| `listener.existingSecret` | `""` | K8s Secret containing the join token |
| `listener.joinTokenKey` | `"join_token"` | Key within the Secret for the join token |
| `listener.runnerNamespace` | `""` | Namespace for runner Jobs (defaults to release namespace) |
| `listener.resources.requests.cpu` | `100m` | CPU request |
| `listener.resources.requests.memory` | `256Mi` | Memory request |
| `listener.autoscaling.enabled` | `false` | Enable HPA for listener |
| `listener.autoscaling.minReplicas` | `1` | HPA minimum replicas |
| `listener.autoscaling.maxReplicas` | `5` | HPA maximum replicas |
| `listener.autoscaling.targetCPUUtilizationPercentage` | `70` | HPA CPU target |
| `listener.pdb.enabled` | `true` | Enable PodDisruptionBudget |
| `listener.pdb.maxUnavailable` | `1` | PDB maxUnavailable (default) |

The listener joins an agent pool on startup using the join token, then connects to the API via SSE (Server-Sent Events) to receive real-time events: run notifications, Job status queries, log streaming requests, and cancellations. The SSE connection is outbound from the listener — it works through firewalls without requiring inbound access. The pool must already exist (created by an admin via the API). To set up the listener:

1. Create an agent pool via the API: `POST /api/v2/organizations/default/agent-pools`
2. Create a join token for the pool: `POST /api/v2/agent-pools/{pool_id}/tokens`
3. Store the raw token in a Kubernetes Secret:

```zsh
kubectl create secret generic terrapod-listener-credentials \
  --namespace terrapod \
  --from-literal=join_token="<raw-token-from-step-2>"
```

4. Configure the listener in values:

```yaml
listener:
  enabled: true
  name: "my-listener"
  existingSecret: terrapod-listener-credentials
```

### Runners

| Value | Default | Description |
|---|---|---|
| `runners.image.repository` | `ghcr.io/mattrobinsonsre/terrapod-runner` | Runner Job image |
| `runners.ttlSecondsAfterFinished` | `600` | Job cleanup TTL |
| `runners.serviceAccount.create` | `true` | Create ServiceAccount for runner Jobs |
| `runners.serviceAccount.name` | `""` | SA name (cloud identity) |
| `runners.serviceAccount.annotations` | `{}` | SA annotations (for IRSA, GCP WIF, Azure WI) |

### Ingress

| Value | Default | Description |
|---|---|---|
| `ingress.enabled` | `false` | Enable ingress |
| `ingress.className` | `""` | Ingress class |
| `ingress.hostname` | `""` | Hostname (required) |
| `ingress.tls` | `true` | Enable TLS |
| `ingress.pathType` | `Prefix` | Ingress path type (`Prefix`, `Exact`, `ImplementationSpecific`) |
| `ingress.annotations` | `{}` | Ingress annotations |
| `ingress.extraPaths` | `[]` | Extra paths prepended before the default catch-all |
| `tls.existingSecret` | `""` | Existing TLS secret name |

### Database

| Value | Default | Description |
|---|---|---|
| `postgresql.url` | `""` | PostgreSQL connection URL |
| `api.config.database.pool_size` | `10` | Persistent connections in pool |
| `api.config.database.max_overflow` | `20` | Extra connections beyond pool_size |
| `api.config.database.pool_pre_ping` | `true` | SELECT 1 before checkout (handles stale connections) |
| `api.config.database.pool_recycle` | `1800` | Recycle connections after N seconds |
| `api.config.database.pool_timeout` | `30` | Seconds to wait for a pool connection |
| `api.config.database.connect_timeout` | `10` | TCP connect timeout in seconds |
| `api.config.database.command_timeout` | `30` | Query timeout in seconds |

### Redis

| Value | Default | Description |
|---|---|---|
| `redis.url` | `""` | Redis connection URL |

### Bootstrap

| Value | Default | Description |
|---|---|---|
| `bootstrap.adminEmail` | | Initial admin email |
| `bootstrap.adminPassword` | | Initial admin password |
| `bootstrap.existingSecret` | | K8s secret with admin credentials |
| `bootstrap.poolName` | `""` | Optional: create an agent pool with this name |
| `bootstrap.poolToken` | `""` | Optional: raw join token for the pool (generated if omitted) |

### Migrations

| Value | Default | Description |
|---|---|---|
| `migrations.enabled` | `true` | Run Alembic migrations on install/upgrade |
| `migrations.image.repository` | `ghcr.io/mattrobinsonsre/terrapod-migrations` | Migrations Docker image |
| `migrations.image.tag` | `""` (appVersion) | Image tag |

---

## Storage Backend Setup

### AWS S3

1. Create an S3 bucket:

```zsh
aws s3 mb s3://terrapod-storage --region eu-west-1
```

2. Create an IAM policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketLocation"
      ],
      "Resource": [
        "arn:aws:s3:::terrapod-storage",
        "arn:aws:s3:::terrapod-storage/*"
      ]
    }
  ]
}
```

3. For EKS, use IRSA (IAM Roles for Service Accounts):

```yaml
api:
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/terrapod-api

  config:
    storage:
      backend: s3
      s3:
        bucket: terrapod-storage
        region: eu-west-1
```

### Azure Blob Storage

1. Create a storage account and container:

```zsh
az storage account create --name terrapodstore --resource-group rg-terrapod --sku Standard_LRS
az storage container create --name terrapod --account-name terrapodstore
```

2. For AKS, use Workload Identity:

```yaml
api:
  serviceAccount:
    annotations:
      azure.workload.identity/client-id: <managed-identity-client-id>

  config:
    storage:
      backend: azure
      azure:
        account_name: terrapodstore
        container_name: terrapod
```

### Google Cloud Storage

1. Create a bucket:

```zsh
gsutil mb -l europe-west1 gs://terrapod-storage
```

2. For GKE, use Workload Identity Federation:

```yaml
api:
  serviceAccount:
    annotations:
      iam.gke.io/gcp-service-account: terrapod@project.iam.gserviceaccount.com

  config:
    storage:
      backend: gcs
      gcs:
        bucket: terrapod-storage
        project_id: your-project-id
```

### Filesystem (PVC)

For environments without cloud object storage:

```yaml
api:
  config:
    storage:
      backend: filesystem

storage:
  filesystem:
    persistence:
      enabled: true
      size: 100Gi
      storageClass: gp3
```

Note: Filesystem storage uses a PVC with `ReadWriteOnce` access mode, which limits API scaling to a single replica (or requires `ReadWriteMany` with a shared filesystem).

---

## Database Setup

Terrapod requires PostgreSQL 14+. Use a managed service (RDS, Cloud SQL, Azure Database) for production.

### Connection URL

The URL uses SQLAlchemy async format:

```
postgresql+asyncpg://username:password@hostname:5432/terrapod
```

### Injecting Credentials

Option 1: Helm value (not recommended for production):

```yaml
postgresql:
  url: "postgresql+asyncpg://terrapod:password@db.example.com:5432/terrapod"
```

Option 2: Kubernetes Secret + environment variable (recommended):

```zsh
kubectl create secret generic terrapod-db-credentials \
  --namespace terrapod \
  --from-literal=database-url="postgresql+asyncpg://terrapod:password@db.example.com:5432/terrapod"
```

Then reference it in an `extraEnv` or by customizing the deployment template with `TERRAPOD_DATABASE_URL`.

### Migrations

Database migrations run automatically as a Helm pre-install/pre-upgrade hook via Alembic. Disable with:

```yaml
migrations:
  enabled: false
```

### Connection Pool Tuning

Terrapod uses SQLAlchemy's async connection pool to manage PostgreSQL connections. The defaults work well for direct connections to a managed database, but you may need to tune them when using a connection pooler (RDS Proxy, pgBouncer) or for high-availability deployments.

| Setting | Default | Description |
|---|---|---|
| `pool_size` | `10` | Number of persistent connections maintained in the pool. Each API replica maintains its own pool, so total connections = `pool_size x replicas` |
| `max_overflow` | `20` | Additional connections allowed beyond `pool_size` under load. These are created on demand and closed when returned to the pool. Maximum connections per replica = `pool_size + max_overflow` |
| `pool_pre_ping` | `true` | Issues a `SELECT 1` before handing out a connection. Detects and discards stale connections (e.g. after a proxy restart or failover). Small latency cost per checkout but prevents connection errors |
| `pool_recycle` | `1800` | Connections older than this (in seconds) are recycled. Set this below your proxy's `max_connection_lifetime` to avoid the proxy forcibly closing connections mid-query |
| `pool_timeout` | `30` | Seconds to wait for a connection from the pool before raising a timeout error. Increase if you see pool exhaustion errors under bursty load |
| `connect_timeout` | `10` | TCP connect timeout in seconds. How long to wait when establishing a new connection to the database |
| `command_timeout` | `30` | Query timeout in seconds. Queries exceeding this are cancelled by asyncpg |

**When to tune these settings:**

- **RDS Proxy / pgBouncer**: Set `pool_recycle` below the proxy's connection lifetime. RDS Proxy defaults to 1800s, so use `1700` to recycle before the proxy does. Keep `pool_pre_ping: true` to handle proxy-side disconnects
- **Multiple API replicas**: Total connections = `(pool_size + max_overflow) x replicas`. With 3 replicas and defaults (10 + 20), that is 90 max connections. Ensure your database's `max_connections` accommodates this plus connections from migrations, monitoring, and other clients
- **High-concurrency workloads**: Increase `pool_size` if you consistently see pool exhaustion. Increase `max_overflow` for bursty traffic patterns
- **Cross-region or high-latency databases**: Increase `connect_timeout` beyond 10s. Consider increasing `command_timeout` for complex queries
- **Failover / HA**: Keep `pool_pre_ping: true` (the default). After a database failover, stale connections to the old primary are detected and discarded on next checkout

**Example: RDS Proxy**

```yaml
api:
  config:
    database:
      pool_size: 5
      max_overflow: 10
      pool_pre_ping: true
      pool_recycle: 1700     # Below RDS Proxy's default 1800s max_connection_lifetime
      pool_timeout: 30
      connect_timeout: 10
      command_timeout: 30
```

With RDS Proxy, you can use a smaller `pool_size` because the proxy maintains its own connection pool to the database. The proxy handles connection multiplexing, so fewer persistent connections per replica are needed.

**Example: pgBouncer (transaction mode)**

```yaml
api:
  config:
    database:
      pool_size: 5
      max_overflow: 10
      pool_pre_ping: true
      pool_recycle: 600      # pgBouncer default server_lifetime is 3600s; recycle well below
      pool_timeout: 30
      connect_timeout: 10
      command_timeout: 30
```

When using pgBouncer in transaction mode, each query gets its own server connection for the duration of the transaction. Use a smaller `pool_size` since pgBouncer multiplexes connections. Set `pool_recycle` well below pgBouncer's `server_lifetime` setting.

**Example: Direct connection (high concurrency)**

```yaml
api:
  config:
    database:
      pool_size: 20
      max_overflow: 30
      pool_pre_ping: true
      pool_recycle: 1800
      pool_timeout: 60
      connect_timeout: 10
      command_timeout: 60
```

For direct database connections under high load, increase `pool_size` and `max_overflow`. With 3 replicas this allows up to 150 simultaneous connections -- ensure your database's `max_connections` is set accordingly (typically 200+ with headroom for admin connections).

---

## Redis Setup

Terrapod requires Redis 7+. Use a managed service (ElastiCache, Memorystore, Azure Cache) for production.

### Connection URL

```
redis://hostname:6379
redis://:password@hostname:6379
rediss://hostname:6380  # TLS
```

### Injecting Credentials

Same pattern as PostgreSQL -- use a Kubernetes Secret with `TERRAPOD_REDIS_URL`.

---

## TLS / Ingress Configuration

### With cert-manager

```yaml
ingress:
  enabled: true
  hostname: terrapod.example.com
  className: nginx
  tls: true
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
```

### With Existing Certificate

```yaml
ingress:
  enabled: true
  hostname: terrapod.example.com
  className: nginx
  tls: true

tls:
  existingSecret: terrapod-tls
```

Create the secret:

```zsh
kubectl create secret tls terrapod-tls \
  --cert=tls.crt \
  --key=tls.key \
  --namespace terrapod
```

### Ingress Controller Notes

The Ingress routes all traffic to the web (Next.js) service. The web service proxies API calls internally. No special path-based routing is needed at the ingress level.

---

## Encryption at Rest

Terrapod delegates encryption at rest to the underlying infrastructure services. No application-level encryption key is needed.

### What to encrypt

| Data | Where it lives | How to encrypt |
|---|---|---|
| Sensitive variables, VCS tokens, user credentials | PostgreSQL | Enable encryption on your managed database (RDS encryption, Cloud SQL encryption, Azure Database encryption) |
| State files, configuration tarballs, plan outputs, logs | Object storage | Enable server-side encryption (S3 SSE-S3/SSE-KMS, Azure Storage encryption, GCS default encryption) |
| Filesystem-backed storage (dev/non-cloud) | PVC | Use encrypted volumes at the infrastructure level |

### AWS example

- **RDS**: Enable `StorageEncrypted: true` (default for new instances) with an AWS KMS key
- **S3**: Enable default bucket encryption with SSE-S3 or SSE-KMS:

```zsh
aws s3api put-bucket-encryption --bucket terrapod-storage \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"aws:kms"}}]}'
```

### Azure example

- **Azure Database for PostgreSQL**: Storage encryption is enabled by default with Microsoft-managed keys
- **Azure Blob Storage**: Encryption at rest is enabled by default with Microsoft-managed keys (optionally use customer-managed keys via Key Vault)

### GCP example

- **Cloud SQL**: Encryption at rest is enabled by default with Google-managed keys (optionally use CMEK via Cloud KMS)
- **GCS**: All objects are encrypted at rest by default with Google-managed keys (optionally use CMEK)

---

## Scaling Considerations

### API Server

The API server is stateless and scales horizontally:

```yaml
api:
  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 10
    targetCPUUtilizationPercentage: 70
```

PodDisruptionBudgets are enabled by default for all Deployments (API, listener, web) with `maxUnavailable: 1`:

```yaml
api:
  pdb:
    enabled: true
    maxUnavailable: 1
```

### Web UI

The web frontend is also stateless:

```yaml
web:
  replicas: 2
```

### Runner Listener

Multiple replicas are supported for high availability. Each pod registers independently with a unique name derived from its pod hostname and competes for queued runs via atomic Postgres locking. A single replica is sufficient for small deployments.

```yaml
listener:
  replicas: 2
```

### Pod Anti-Affinity

By default, all three Deployments (API, listener, web) are configured with pod anti-affinity rules to spread replicas for high availability:

- **Required node anti-affinity** — pods of the same component must be scheduled on different nodes (`kubernetes.io/hostname`)
- **Preferred AZ anti-affinity** — pods of the same component should be spread across availability zones (`topology.kubernetes.io/zone`)

This is controlled per component via `podAntiAffinity.enabled` (default: `true`):

```yaml
api:
  podAntiAffinity:
    enabled: true   # default

web:
  podAntiAffinity:
    enabled: true   # default

listener:
  podAntiAffinity:
    enabled: true   # default
```

**Disable** on single-node clusters (e.g. local development):

```yaml
api:
  podAntiAffinity:
    enabled: false
```

**Override** with custom affinity rules by setting the `affinity` field. When `affinity` is non-empty, it completely replaces the auto-generated anti-affinity:

```yaml
api:
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: node-role
                operator: In
                values: ["api"]
```

### Runner Jobs

Runner Jobs are ephemeral and scale naturally. Configure workspace-level resource limits:

- Default: 1 CPU request / 2 CPU limit, 2Gi memory request / 4Gi memory limit
- Adjust per workspace via `resource-cpu` and `resource-memory` attributes

### Database

PostgreSQL is the bottleneck for high-concurrency scenarios. Use:
- Connection pooling (PgBouncer or RDS Proxy) for large deployments -- see [Connection Pool Tuning](#connection-pool-tuning) for recommended settings
- Read replicas for read-heavy workloads
- Appropriately sized instance for your run volume
- Total max connections = `(pool_size + max_overflow) x replicas` -- ensure your database's `max_connections` accommodates this

### Redis

Redis handles sessions, auth state, and listener heartbeats. A single Redis instance or small cluster is typically sufficient.

---

## Monitoring and Health Checks

### Liveness Probe

```
GET /health
```

Returns 200 if the process is running. Used by Kubernetes liveness probes.

### Readiness Probe

```
GET /ready
```

Checks database, Redis, and storage. Returns 200 if all subsystems are healthy, 503 otherwise. Used by Kubernetes readiness probes to remove unhealthy pods from service.

### Structured Logging

Terrapod uses structlog for JSON-formatted logs in production:

```yaml
api:
  config:
    log_level: info
```

Logs are written to stdout in JSON format, suitable for log aggregation (Fluentd, Loki, CloudWatch, etc.).

### Health Dashboard

The admin health dashboard endpoint (`GET /api/v2/admin/health-dashboard`) provides a single-request overview of platform health, including workspace drift status, recent run statistics, and listener availability. This is useful for integration with external monitoring dashboards (Grafana, Datadog, etc.) or custom alerting.

Requires `admin` or `audit` role. See the [API Reference](api-reference.md#health-dashboard) for the full response schema.

### Prometheus Metrics

When `api.config.metrics.enabled` is true (default), the API server exposes a `/metrics` endpoint in Prometheus exposition format. The listener also serves `/metrics` on its health port. See the [Metrics](#metrics) configuration reference for available metrics and ServiceMonitor/PodMonitor setup.

### Key Metrics to Monitor

| Metric | Where to Find |
|---|---|
| API request latency | `terrapod_http_request_duration_seconds` (Prometheus) or ingress controller metrics |
| API request rate | `terrapod_http_requests_total` (Prometheus) |
| Listener active runs | `terrapod_listener_active_runs` (Prometheus) |
| Listener heartbeat age | `terrapod_listener_heartbeat_age_seconds` (Prometheus) |
| Run queue depth | Count runs in `queued` state via API or health dashboard |
| Drift status | Health dashboard `workspaces.by-drift-status` |
| Database connections | PostgreSQL `pg_stat_activity` |
| Storage operations | Cloud provider metrics (S3/Blob/GCS) |
| Job success/failure | Kubernetes Job status in runner namespace |

---

## Helm Chart Templates

The chart includes these templates in `helm/terrapod/templates/`:

| Template | Resource |
|---|---|
| `configmap-api.yaml` | API config.yaml ConfigMap |
| `configmap-runner.yaml` | Runner configuration ConfigMap |
| `deployment-api.yaml` | API Deployment |
| `deployment-listener.yaml` | Runner listener Deployment |
| `deployment-web.yaml` | Web UI Deployment |
| `service-api.yaml` | API Service |
| `service-web.yaml` | Web UI Service |
| `ingress.yaml` | Ingress (BFF pattern) |
| `rbac-listener.yaml` | Listener ServiceAccount, Role, RoleBinding |
| `serviceaccount.yaml` | API ServiceAccount |
| `serviceaccount-web.yaml` | Web UI ServiceAccount |
| `serviceaccount-runner.yaml` | Runner ServiceAccount |
| `job-migrations.yaml` | Alembic migrations (pre-install/pre-upgrade hook) |
| `job-bootstrap.yaml` | Admin user bootstrap (post-install hook) |
| `pvc-storage.yaml` | PVC for filesystem backend |
| `pdb-api.yaml` | API PodDisruptionBudget |
| `pdb-listener.yaml` | Listener PodDisruptionBudget |
| `pdb-web.yaml` | Web PodDisruptionBudget |
| `hpa-api.yaml` | API HorizontalPodAutoscaler |
| `hpa-listener.yaml` | Listener HorizontalPodAutoscaler |
| `hpa-web.yaml` | Web HorizontalPodAutoscaler |
| `servicemonitor-api.yaml` | Prometheus ServiceMonitor (disabled by default) |
| `podmonitor-listener.yaml` | Prometheus PodMonitor (disabled by default) |
