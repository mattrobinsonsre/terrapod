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

Reduce the maximum API token lifetime. The default is 8760 hours (1 year); `0` means no limit. For stricter environments:

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

### Dedicated token signing key

One key signs four families of stateless token — runner tokens, run-task
callback tokens, download tickets and Slack link tokens. Without one it is
derived from `sha256(database_url)`, which is the wrong material for the job: a
database URL is a credential for another system, handed to every client of that
database, to backup and migration jobs, and to a DBA who has no business
forging platform tokens.

A fresh `helm install` generates a key for you. An **upgrade never does**, and
that asymmetry is deliberate: a new key invalidates every token already in
flight, and those tokens are what running plans and applies authenticate with.
A deployment upgrading into this keeps the key it already had and warns at
startup until you supply one.

Supply your own via `api.tokenSigningKey` (injected as
`TERRAPOD_TOKEN_SIGNING_KEY` from a K8s Secret, never a ConfigMap):

```zsh
kubectl -n terrapod create secret generic terrapod-token-signing \
  --from-literal=token_signing_key="$(openssl rand -hex 32)"
```

```yaml
api:
  tokenSigningKey:
    existingSecret: "terrapod-token-signing"
    existingSecretKey: "token_signing_key"
```

Naming a Secret this way stops the chart generating anything, so the key
changes only when you change it.

Rotating it has to roll every API pod at once. Environment from a
`secretKeyRef` is captured when a pod starts and never refreshes, so a fleet
that disagrees about the key is not a clean failure — it serves `401` on
whatever share of requests lands on a pod holding the other one, which for a
run means roughly half its API calls fail while the rest succeed. Under `helm
install`/`helm upgrade` the chart handles this: the pod template carries a
checksum over the key, so a change rolls the Deployment. That checksum is built
from a cluster `lookup`, so **a renderer cannot produce it** — see below.

#### Under a GitOps controller, own the Secret yourself

If you deploy the chart through Argo CD, Flux, or anything else that renders
with `helm template` and reconciles the result, **set
`api.tokenSigningKey.existingSecret`**. Do not rely on the generated key.

Two properties of `helm template` rule it out there. It cannot read the
cluster, so the chart cannot tell whether a key already exists — it emits
nothing rather than mint a fresh one on every render, which would rotate the
key underneath a running deployment. And a rendered manifest that omits the
Secret reads, to a controller configured to prune, as an instruction to delete
it. `helm.sh/resource-policy: keep` does not prevent that: it is an annotation
Helm honours, and a GitOps controller is not Helm.

So treat the Secret as yours — create it out of band (an External Secrets
Operator `ExternalSecret` from your cloud secret manager, a sealed secret, or
whatever your tooling uses) and keep it outside anything that prunes. Express
that however your controller expresses it; Terrapod does not ship
vendor-specific annotations for it.

Rotation is yours too. The `checksum/token-signing` annotation that rolls the
Deployment on a key change is computed by reading the Secret from the cluster,
which a renderer cannot do, so it is absent from a rendered manifest. **After
changing the key, restart the API yourself** — otherwise the old pods keep the
old key and the fleet splits:

```zsh
kubectl -n terrapod rollout restart deploy/<release>-api
```

If the Secret is pruned anyway, the deployment does not fail cleanly. Pods
already running keep the key in their environment, while any pod started
afterwards falls back to the derived one, and requests return `401` wherever
they land on the wrong half — until the last pod holding the old key retires,
at which point the fleet agrees again on the weaker derived key without saying
so. Recreate the Secret and roll the API.

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

Auth endpoints (`/api/terrapod/v1/auth/*`, `/oauth/*`) have a separate, lower limit to protect against credential stuffing. Health, readiness, and metrics endpoints are exempt.

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
  "https://terrapod.example.com/api/terrapod/v1/admin/audit-log?page[size]=100"
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
