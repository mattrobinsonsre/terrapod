# Production Deployment

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

---

## Helm Chart Installation

### Basic Install

```zsh
helm install terrapod ./helm/terrapod \
  --namespace terrapod \
  --create-namespace \
  --set ingress.enabled=true \
  --set ingress.hostname=terrapod.example.com \
  --set postgresql.url="postgresql+asyncpg://terrapod:PASSWORD@db.example.com:5432/terrapod" \
  --set redis.url="redis://redis.example.com:6379"
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
      module_cache:
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

ingress:
  enabled: true
  hostname: terrapod.example.com
  className: nginx
  tls: true
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod

runners:
  default: standard
  defaultTerraformVersion: "1.9.8"
  defaultExecutionBackend: terraform
  serviceAccountName: terrapod-runner

postgresql:
  url: ""  # Injected via secret

redis:
  url: ""  # Injected via secret

bootstrap:
  adminEmail: admin@example.com
  existingSecret: terrapod-admin-credentials
```

```zsh
helm install terrapod ./helm/terrapod \
  --namespace terrapod \
  --create-namespace \
  -f values-production.yaml
```

---

## Configuration Reference

### Global

| Value | Default | Description |
|---|---|---|
| `global.imagePullSecrets` | `[]` | Image pull secrets for private registries |

### API Server

| Value | Default | Description |
|---|---|---|
| `api.replicas` | `2` | Number of API server replicas |
| `api.image.repository` | `ghcr.io/mattrobinsonsre/terrapod-api` | API Docker image |
| `api.image.tag` | `""` (appVersion) | Image tag |
| `api.resources.requests.cpu` | `250m` | CPU request |
| `api.resources.requests.memory` | `512Mi` | Memory request |
| `api.resources.limits.cpu` | `1` | CPU limit |
| `api.resources.limits.memory` | `1Gi` | Memory limit |
| `api.autoscaling.enabled` | `true` | Enable HPA |
| `api.autoscaling.minReplicas` | `2` | HPA minimum replicas |
| `api.autoscaling.maxReplicas` | `10` | HPA maximum replicas |
| `api.autoscaling.targetCPUUtilizationPercentage` | `70` | HPA target CPU |
| `api.pdb.enabled` | `true` | Enable PodDisruptionBudget |
| `api.pdb.minAvailable` | `1` | PDB minimum available |
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
| `web.resources.requests.memory` | `128Mi` | Memory request |

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
| `api.config.auth.api_token_max_ttl_hours` | `0` | Max API token lifetime (0 = no limit) |
| `api.config.auth.sso.default_provider` | `""` | Default SSO provider name |
| `api.config.auth.sso.oidc` | `[]` | OIDC provider configurations |
| `api.config.auth.sso.saml` | `[]` | SAML provider configurations |

### Registry & Caching

| Value | Default | Description |
|---|---|---|
| `api.config.registry.enabled` | `true` | Enable private registry |
| `api.config.registry.module_cache.enabled` | `true` | Enable module caching |
| `api.config.registry.provider_cache.enabled` | `true` | Enable provider caching |
| `api.config.registry.binary_cache.enabled` | `true` | Enable binary caching |

### VCS

| Value | Default | Description |
|---|---|---|
| `api.config.vcs.enabled` | `false` | Enable VCS integration |
| `api.config.vcs.poll_interval_seconds` | `60` | Poll interval |
| `api.config.vcs.github.webhook_secret` | `""` | GitHub webhook HMAC secret |

### Encryption

| Value | Default | Description |
|---|---|---|
| `api.config.encryption_key` | `""` | Fernet encryption key (inject via env var) |

### Runner Listener

| Value | Default | Description |
|---|---|---|
| `listener.enabled` | `true` | Enable local runner listener |
| `listener.replicas` | `1` | Number of listener replicas |
| `listener.runnerNamespace` | `""` | Namespace for runner Jobs (defaults to release namespace) |
| `listener.resources.requests.cpu` | `100m` | CPU request |
| `listener.resources.requests.memory` | `256Mi` | Memory request |

### Runners

| Value | Default | Description |
|---|---|---|
| `runners.default` | `standard` | Default runner definition name |
| `runners.image.repository` | `ghcr.io/mattrobinsonsre/terrapod-runner` | Runner Job image |
| `runners.defaultTerraformVersion` | `1.9.8` | Default terraform version |
| `runners.defaultExecutionBackend` | `terraform` | Default backend (`terraform` or `tofu`) |
| `runners.serviceAccountName` | `""` | SA for runner Jobs (cloud identity) |
| `runners.ttlSecondsAfterFinished` | `600` | Job cleanup TTL |
| `runners.definitions` | See values.yaml | Named runner definitions |

### Ingress

| Value | Default | Description |
|---|---|---|
| `ingress.enabled` | `false` | Enable ingress |
| `ingress.className` | `""` | Ingress class |
| `ingress.hostname` | `""` | Hostname (required) |
| `ingress.tls` | `true` | Enable TLS |
| `ingress.annotations` | `{}` | Ingress annotations |
| `tls.existingSecret` | `""` | Existing TLS secret name |

### Database & Redis

| Value | Default | Description |
|---|---|---|
| `postgresql.url` | `""` | PostgreSQL connection URL |
| `redis.url` | `""` | Redis connection URL |

### Bootstrap

| Value | Default | Description |
|---|---|---|
| `bootstrap.adminEmail` | | Initial admin email |
| `bootstrap.adminPassword` | | Initial admin password |
| `bootstrap.existingSecret` | | K8s secret with credentials |

### Migrations

| Value | Default | Description |
|---|---|---|
| `migrations.enabled` | `true` | Run Alembic migrations on install/upgrade |

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

## Encryption Key

The Fernet encryption key protects:
- Sensitive variables
- VCS connection tokens (GitHub private keys, GitLab tokens)
- State files at rest

Generate a key:

```zsh
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Store it in a Kubernetes Secret and inject via environment variable:

```zsh
kubectl create secret generic terrapod-encryption \
  --namespace terrapod \
  --from-literal=encryption-key="your-fernet-key"
```

Reference it as `TERRAPOD_ENCRYPTION__KEY` in the API deployment.

Without an encryption key:
- Sensitive variables are rejected
- State files are stored unencrypted
- VCS connections cannot be created

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

The PodDisruptionBudget ensures at least one replica is available during rolling updates:

```yaml
api:
  pdb:
    enabled: true
    minAvailable: 1
```

### Web UI

The web frontend is also stateless:

```yaml
web:
  replicas: 2
```

### Runner Listener

A single listener replica is typically sufficient. It polls for queued runs and creates K8s Jobs -- the heavy work happens in the Jobs themselves.

```yaml
listener:
  replicas: 1
```

### Runner Jobs

Runner Jobs are ephemeral and scale naturally. Configure workspace-level resource limits:

- Default: 1 CPU request / 2 CPU limit, 2Gi memory request / 4Gi memory limit
- Adjust per workspace via `resource-cpu` and `resource-memory` attributes

### Database

PostgreSQL is the bottleneck for high-concurrency scenarios. Use:
- Connection pooling (PgBouncer) for large deployments
- Read replicas for read-heavy workloads
- Appropriately sized instance for your run volume

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

### Key Metrics to Monitor

| Metric | Where to Find |
|---|---|
| API request latency | Ingress controller metrics or application logs |
| Run queue depth | Count runs in `queued` state via API |
| Listener heartbeat | Redis keys `tp:listener:{id}:status` |
| Database connections | PostgreSQL `pg_stat_activity` |
| Storage operations | Cloud provider metrics (S3/Blob/GCS) |
| Job success/failure | Kubernetes Job status in runner namespace |

---

## Helm Chart Templates

The chart includes these templates in `helm/terrapod/templates/`:

| Template | Resource |
|---|---|
| `configmap-api.yaml` | API config.yaml ConfigMap |
| `configmap-runner.yaml` | Runner definitions ConfigMap |
| `deployment-api.yaml` | API Deployment |
| `deployment-listener.yaml` | Runner listener Deployment |
| `deployment-web.yaml` | Web UI Deployment |
| `service-api.yaml` | API Service |
| `service-web.yaml` | Web UI Service |
| `ingress.yaml` | Ingress (BFF pattern) |
| `rbac-listener.yaml` | Listener ServiceAccount, Role, RoleBinding |
| `serviceaccount.yaml` | API ServiceAccount |
| `job-migrations.yaml` | Alembic migrations (pre-install/pre-upgrade hook) |
| `job-bootstrap.yaml` | Admin user bootstrap (post-install hook) |
| `pvc-storage.yaml` | PVC for filesystem backend |
| `pdb-api.yaml` | PodDisruptionBudget |
