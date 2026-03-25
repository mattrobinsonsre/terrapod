# Security Hardening Guide

This guide covers production security hardening for Terrapod deployments. It assumes you have a working Terrapod installation and want to tighten its security posture.

## TLS Configuration

### Database (PostgreSQL)

Use `sslmode=verify-full` in the database connection URL to enforce TLS with certificate validation:

```yaml
postgresql:
  url: "postgresql+asyncpg://terrapod:password@db.example.com:5432/terrapod?ssl=verify-full"
```

For managed databases (RDS, Cloud SQL, Azure Database), enable SSL enforcement at the provider level and provide the CA certificate bundle.

### Redis

Use `rediss://` (note the double `s`) to enforce TLS on Redis connections:

```yaml
redis:
  url: "rediss://default:password@redis.example.com:6380"
```

For ElastiCache, MemoryDB, or Azure Cache for Redis, enable in-transit encryption at the provider level.

### Ingress

Always terminate TLS at the ingress controller. Provide a valid certificate:

```yaml
ingress:
  enabled: true
  hostname: terrapod.example.com
  tls: true
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
```

## Authentication Hardening

### Disable Local Authentication

When an SSO provider (OIDC/SAML) is configured, disable local password authentication to enforce centralized identity management:

```yaml
api:
  config:
    auth:
      local_enabled: false
      sso:
        default_provider: "your-idp"
```

### API Token Lifetime

Reduce the maximum API token lifetime. The default is 168 hours (7 days). For stricter environments:

```yaml
api:
  config:
    auth:
      api_token_max_ttl_hours: 24  # Tokens expire after 24 hours
```

### Require SSO for Privileged Roles

Force specific roles to authenticate via external SSO (not local passwords):

```yaml
api:
  config:
    auth:
      require_external_sso_for_roles:
        - admin
        - audit
```

## Secrets Management

### Use Kubernetes Secrets

Never put credentials directly in `values.yaml`. Use `existingSecret` references:

```yaml
postgresql:
  existingSecret: "terrapod-db-credentials"
  existingSecretKey: "url"

redis:
  existingSecret: "terrapod-redis-credentials"
  existingSecretKey: "url"
```

For SSO provider client secrets, create a K8s Secret and reference it:

```bash
kubectl create secret generic terrapod-oidc \
  --from-literal=client_secret=<your-secret>
```

```yaml
api:
  config:
    auth:
      sso:
        oidc:
          - name: "your-idp"
            existingSecret: "terrapod-oidc"
            existingSecretKey: "client_secret"
```

### External Secrets Operator

For production, use [External Secrets Operator](https://external-secrets.io/) to sync secrets from AWS Secrets Manager, Azure Key Vault, or GCP Secret Manager into Kubernetes Secrets automatically.

## Network Policies

Terrapod ships with NetworkPolicy templates that restrict pod-to-pod and pod-to-external traffic. Enable them:

```yaml
networkPolicies:
  enabled: true
```

This creates four NetworkPolicies:

| Policy | Ingress | Egress |
|--------|---------|--------|
| **api** | Web, listener, runner on port 8000 | Postgres (5432), Redis (6379), HTTPS (443), DNS |
| **web** | Ingress controller on port 3000 | API (8000), DNS |
| **listener** | None | API (8000), K8s API (443), DNS |
| **runner** | None | API (8000), HTTPS (443), DNS |

Runners are explicitly denied access to Postgres and Redis.

**Prerequisite:** Your cluster must have a CNI plugin that supports NetworkPolicy (Calico, Cilium, Weave Net, etc.).

## Pod Security Standards

Use Kubernetes Pod Security Standards to enforce security contexts at the namespace level:

```yaml
namespace:
  create: true
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

Terrapod's default pod and container security contexts are already compatible with the `restricted` profile:
- `runAsNonRoot: true`
- `readOnlyRootFilesystem: true`
- `allowPrivilegeEscalation: false`
- `capabilities.drop: [ALL]`
- `seccompProfile.type: RuntimeDefault`

## Rate Limiting

API rate limiting is enabled by default to protect against brute-force and denial-of-service attacks. The default configuration:

```yaml
api:
  config:
    rate_limit:
      enabled: true
      requests_per_minute: 100
      auth_requests_per_minute: 10
```

Auth endpoints (`/api/v2/auth/*`, `/oauth/*`) have a separate, lower limit to protect against credential stuffing. Health, readiness, and metrics endpoints are exempt.

Rate limiting uses Redis for distributed counting across replicas and fails open if Redis is unavailable.

## Audit Logging

### Retention

Configure audit log retention based on your compliance requirements:

```yaml
api:
  config:
    audit:
      retention_days: 365  # 1 year for SOC2/ISO27001
```

### SIEM Export

Query the audit log API and forward events to your SIEM:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "https://terrapod.example.com/api/v2/admin/audit-log?page[size]=100"
```

Integrate with your log aggregator (Elasticsearch, Splunk, Datadog) by polling this endpoint periodically.

## Database Hardening

- **Encryption at rest**: Enable at the provider level (RDS encryption, Cloud SQL encryption, Azure Database encryption)
- **Network isolation**: Place the database in a private subnet with no public access
- **Credential rotation**: Use IAM database authentication (RDS) or workload identity (Cloud SQL, Azure) instead of static passwords
- **Connection pooling**: Use PgBouncer or a managed connection pool to limit concurrent connections and prevent connection exhaustion

## Runner Isolation

Runner Jobs execute untrusted Terraform/Tofu code. Harden them:

- **Short-lived runner tokens**: Each runner Job receives an HMAC-signed token scoped to its specific `run_id` with a configurable TTL (default 1h, max 2h). The token is stored in a K8s Secret with `ownerReference` to the Job — automatically garbage-collected when the Job is cleaned up. The raw token never appears in the Job spec (injected via `secretKeyRef`)
- **Principle of least privilege**: Runner tokens carry only the `everyone` role. They can access binary cache downloads, provider mirror, and artifact endpoints for their own run — nothing else. Admin, write, and CRUD endpoints are inaccessible
- **Authenticated API access**: All runner-facing endpoints (binary cache, provider mirror, artifact upload/download) require authentication. There are no unauthenticated endpoints that serve cached binaries or provider packages
- **Read-only root filesystem**: Enabled by default. Writable directories (`/workspace`, `/tmp`) use emptyDir volumes
- **No service account token**: `automountServiceAccountToken: false` by default (unless CSP identity is needed)
- **Non-root execution**: Runs as UID 1000
- **Dropped capabilities**: All Linux capabilities dropped
- **Seccomp profile**: RuntimeDefault
- **Resource limits**: CPU and memory limits prevent noisy-neighbor issues
- **Network isolation**: NetworkPolicies deny access to Postgres and Redis

### Runner Token TTL

Tune token lifetimes based on your typical run duration:

```yaml
runners:
  tokenTTLSeconds: 3600       # Default token lifetime (1 hour)
  maxTokenTTLSeconds: 7200    # Hard ceiling — API rejects requests above this
```

For environments with fast runs, reduce the TTL to minimize the window of token validity. For long-running applies (large infrastructure), increase as needed.

For additional isolation, use:

```yaml
runners:
  nodeSelector:
    node-role.kubernetes.io/runner: "true"
  tolerations:
    - key: "runner"
      operator: "Exists"
      effect: "NoSchedule"
```

This schedules runner Jobs on dedicated nodes, isolating them from the control plane.

## Object Storage

- **Encryption at rest**: Enable SSE-S3/SSE-KMS (AWS), Azure Storage encryption, or GCS default encryption
- **Access logging**: Enable S3 access logging, Azure Storage analytics, or GCS audit logging
- **Bucket policy**: Restrict access to the Terrapod service account only
- **Versioning**: Enable object versioning for state file recovery

## Backup Strategy

- **PostgreSQL**: Daily automated backups with point-in-time recovery (PITR). Managed databases (RDS, Cloud SQL) handle this automatically
- **Object storage**: Enable versioning. Cross-region replication for disaster recovery
- **Redis**: Ephemeral by design (sessions, cache). No backup needed — data is reconstructed on restart
- **Secrets**: Back up Kubernetes Secrets to your secrets manager. They are the most critical non-reconstructable data
