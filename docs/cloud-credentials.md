# Cloud Credentials

Terrapod reaches cloud APIs — both the platform's own object storage **and** the cloud resources your Terraform manages — using **Kubernetes workload identity**. No long-lived access keys, secrets, or service-account JSON files are stored in Terrapod, in Helm values, or in the database. The platform and every run authenticate with short-lived, automatically-rotated, auditable credentials.

This page is both a primer (if IRSA/WIF/WI are new to you) and a reference (copy-paste bundles per cloud, plus troubleshooting). If you already know workload identity, jump to the [per-cloud bundles](#aws-irsa-setup).

---

## Primer: What is workload identity, and why you want it

A pod that needs to call a cloud API has to prove who it is. The old way was to mount a static credential — an AWS access-key pair, a GCP service-account JSON key, an Azure client secret — into the pod as a file or environment variable. Those credentials are long-lived, easy to leak, hard to rotate, and rarely scoped tightly.

**Workload identity** replaces the static credential with a short-lived token derived from the pod's Kubernetes ServiceAccount (SA). The flow is the same idea on every cloud:

1. The Kubernetes cluster runs an **OIDC provider** that issues a signed token (a JWT) describing the pod's SA — its `sub` claim is `system:serviceaccount:<namespace>:<sa-name>`.
2. You configure the cloud to **trust** that OIDC provider for a specific SA, and map it to a cloud IAM role/identity.
3. When the pod calls the cloud, the cloud verifies the SA token against the trust configuration and hands back a **short-lived credential** (typically ~1 hour, auto-refreshed) for the mapped role.

Why this is worth the setup:

- **No static keys** — there is no secret to leak, commit, or rotate. The cluster mints tokens on demand.
- **Short-lived** — credentials expire in minutes-to-an-hour and refresh transparently; a stolen token is useless quickly.
- **Auditable** — cloud audit logs show "this SA assumed this role", tying every API call back to a named workload.
- **Tightly scoped** — each SA maps to its own role, so the API's storage role and a runner's workload role are independent and least-privilege.

Each cloud has its own name for the mechanism:

| Cloud | Mechanism | What the pod gets |
|---|---|---|
| **AWS** | IRSA — IAM Roles for Service Accounts | An assumed IAM role via `AssumeRoleWithWebIdentity` |
| **GCP** | Workload Identity Federation (GKE) | Impersonation of a GCP service account |
| **Azure** | Workload Identity | A federated token exchanged for a Managed Identity token |

Terraform's `aws`, `google`, and `azurerm` providers all pick these credentials up automatically — there is nothing to configure in your `.tf` code.

### Decision tree: which mechanism do I use?

```
Where does the pod run?
│
├─ Amazon EKS ─────────────► AWS IRSA
│                            (SA annotation: eks.amazonaws.com/role-arn)
│
├─ Google GKE ─────────────► GCP Workload Identity Federation
│                            (SA annotation: iam.gke.io/gcp-service-account)
│
├─ Azure AKS ──────────────► Azure Workload Identity
│                            (SA annotation: azure.workload.identity/client-id
│                             + pod label azure.workload.identity/use: "true")
│
└─ Self-managed / other ───► Any cluster with a reachable OIDC issuer can
   Kubernetes                use the matching cloud's federation (e.g. an
                             on-prem cluster federated to AWS IAM via its
                             own OIDC discovery URL). The SA-annotation
                             webhooks above are EKS/GKE/AKS conveniences;
                             the underlying OIDC federation is portable.
```

If runs need to reach a **different** cloud or account than the one the cluster lives in, that is still workload identity — you grant the runner SA's role permission to assume a role in the target account (AWS cross-account `sts:AssumeRole`), or federate the cluster's OIDC issuer into the target cloud. Deploy a separate listener Deployment (agent pool) per target where it helps keep roles least-privilege.

---

## Separate Roles for API and Runners

The API and runners have different permission requirements and should use separate IAM roles:

| Component | Purpose | Typical Permissions |
|---|---|---|
| **API** (`api.serviceAccount`) | Terrapod's object storage (state files, configs, logs, registry artifacts) | S3/GCS/Azure Blob read/write on the Terrapod storage bucket |
| **Runner** (`runners.serviceAccount`) | Terraform workload execution — whatever infrastructure your Terraform code manages | Varies by workload (EC2, RDS, Lambda, IAM, Route53, etc.) |

In a split deployment topology:
- **Management cluster** (API + web): only the API SA and its IAM role are needed
- **Agent clusters** (listeners + runners): only the runner SA and its IAM role are needed

The listener Deployment can also carry its own SA annotations when the listener itself needs cloud access (e.g. object storage). See [Listener ServiceAccount](#listener-serviceaccount).

---

## How It Works

Each cloud provider has a Kubernetes integration that projects short-lived credentials into pods based on ServiceAccount annotations:

| Provider | Mechanism | SA Annotation |
|---|---|---|
| **AWS** | IRSA (IAM Roles for Service Accounts) | `eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/...` |
| **GCP** | Workload Identity Federation | `iam.gke.io/gcp-service-account: ...@project.iam.gserviceaccount.com` |
| **Azure** | Workload Identity | `azure.workload.identity/client-id: <managed-identity-client-id>` |

When a pod starts, the cloud provider's mutating admission webhook injects the necessary environment variables and token volumes. Terraform providers (aws, google, azurerm) pick up these credentials automatically.

The platform's object-storage backends are workload-identity-native and hold **no** static keys:

- **AWS S3** — via `aioboto3`, which resolves credentials through the standard AWS credential chain (IRSA web-identity token).
- **Azure Blob** — via `DefaultAzureCredential`, which resolves the federated Workload Identity token.
- **GCS** — via Application Default Credentials (Workload Identity Federation on GKE).

---

## ServiceAccount Configuration

### API ServiceAccount

The API server uses its own ServiceAccount for object storage access:

```yaml
api:
  serviceAccount:
    create: true
    annotations:
      eks.amazonaws.com/role-arn: "arn:aws:iam::123456789012:role/terrapod-api-my-cluster"
```

### Runner ServiceAccount

Runner Jobs use a separate ServiceAccount for Terraform workload permissions:

| Priority | Source | Configured Via |
|---|---|---|
| 1 | **Global runner SA** | `runners.serviceAccount.name` in Helm values |
| 2 | **K8s default SA** | Implicit namespace default |

```yaml
runners:
  serviceAccount:
    create: true
    name: "terrapod-runner"
    annotations:
      eks.amazonaws.com/role-arn: "arn:aws:iam::123456789012:role/terrapod-runner-my-cluster"
```

The runner SA is only created when `listener.enabled: true` (i.e. on clusters that actually run Jobs).

For multi-cloud or multi-account setups, deploy separate listener Deployments (agent pools) in different clusters or namespaces, each with their own Helm-configured ServiceAccount.

---

## AWS IRSA Setup

### 1. Configure OIDC Provider

Your EKS cluster must have an IAM OIDC provider configured. This is typically set up during cluster creation. Note the provider URL — it looks like `oidc.eks.<region>.amazonaws.com/id/EXAMPLE` and appears in the trust policies below.

### 2. Create IAM Roles

Create separate IAM roles for the API and runner, each with a trust policy scoped to its own ServiceAccount.

**API role** (S3 access for Terrapod storage):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLE"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLE:sub": "system:serviceaccount:terrapod:terrapod",
          "oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLE:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
```

**Runner role** (Terraform workload permissions):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLE"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLE:sub": "system:serviceaccount:terrapod:terrapod-runner",
          "oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLE:aud": "sts.amazonaws.com"
        }
      }
    }
  ]
}
```

The `:sub` condition pins the role to exactly one SA (`<namespace>:<sa-name>`); the `:aud` condition pins it to the AWS STS audience that IRSA tokens are minted with. Both must match or the assume-role is rejected.

### 3. Configure Helm (`values-aws.yaml`)

A complete copy-paste bundle for an EKS deployment:

```yaml
# values-aws.yaml
api:
  serviceAccount:
    create: true
    annotations:
      eks.amazonaws.com/role-arn: "arn:aws:iam::123456789012:role/terrapod-api-my-cluster"
  config:
    storage:
      # Storage backend uses the API role above — no keys needed
      backend: s3
      s3:
        bucket: terrapod-storage-my-cluster
        region: eu-west-1

runners:
  serviceAccount:
    create: true
    name: "terrapod-runner"
    annotations:
      eks.amazonaws.com/role-arn: "arn:aws:iam::123456789012:role/terrapod-runner-my-cluster"
```

The EKS mutating webhook automatically injects `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` environment variables into pods. No pod labels required.

### Managing IAM with AWS Controllers for Kubernetes (ACK)

If your cluster runs the [ACK IAM controller](https://aws-controllers-k8s.github.io/community/docs/community/services/#iam), you can declare IAM Policies and Roles as Kubernetes resources. This keeps IAM definitions version-controlled alongside your Helm chart:

```yaml
apiVersion: iam.services.k8s.aws/v1alpha1
kind: Policy
metadata:
  name: terrapod-api-my-cluster
  annotations:
    services.k8s.aws/adoption-policy: "adopt-or-create"
    services.k8s.aws/region: "eu-west-1"
spec:
  name: terrapod-api-my-cluster
  description: "Terrapod API — S3 object storage access"
  policyDocument: |
    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetBucketLocation"],
          "Resource": ["arn:aws:s3:::terrapod-storage-my-cluster", "arn:aws:s3:::terrapod-storage-my-cluster/*"]
        }
      ]
    }
---
apiVersion: iam.services.k8s.aws/v1alpha1
kind: Role
metadata:
  name: terrapod-api-my-cluster
  annotations:
    services.k8s.aws/adoption-policy: "adopt-or-create"
    services.k8s.aws/region: "eu-west-1"
spec:
  name: terrapod-api-my-cluster
  assumeRolePolicyDocument: |
    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Principal": {
            "Federated": "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLE"
          },
          "Action": "sts:AssumeRoleWithWebIdentity",
          "Condition": {
            "StringEquals": {
              "oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLE:sub": "system:serviceaccount:terrapod:terrapod"
            }
          }
        }
      ]
    }
  policies:
    - "arn:aws:iam::123456789012:policy/terrapod-api-my-cluster"
```

The `adopt-or-create` annotation allows ACK to adopt existing IAM resources or create new ones. See the [ACK IAM controller documentation](https://aws-controllers-k8s.github.io/community/reference/iam/v1alpha1/role/) for full reference.

---

## GCP Workload Identity Federation Setup

### 1. Create GCP Service Accounts

```bash
# API SA — storage access
gcloud iam service-accounts create terrapod-api --project=my-project

# Runner SA — workload permissions
gcloud iam service-accounts create terrapod-runner --project=my-project
```

### 2. Grant Permissions

```bash
# API: object storage
gcloud projects add-iam-policy-binding my-project \
  --member="serviceAccount:terrapod-api@my-project.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin" \
  --condition="expression=resource.name.startsWith('projects/_/buckets/terrapod-storage'),title=terrapod-bucket"

# Runner: whatever your Terraform code needs
gcloud projects add-iam-policy-binding my-project \
  --member="serviceAccount:terrapod-runner@my-project.iam.gserviceaccount.com" \
  --role="roles/editor"
```

### 3. Bind K8s SAs to GCP SAs

```bash
gcloud iam service-accounts add-iam-policy-binding \
  terrapod-api@my-project.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="serviceAccount:my-project.svc.id.goog[terrapod/terrapod]"

gcloud iam service-accounts add-iam-policy-binding \
  terrapod-runner@my-project.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="serviceAccount:my-project.svc.id.goog[terrapod/terrapod-runner]"
```

The `[terrapod/terrapod]` member is `[<namespace>/<k8s-sa-name>]` — it must match the Kubernetes SA exactly.

### 4. Configure Helm (`values-gcp.yaml`)

```yaml
# values-gcp.yaml
api:
  serviceAccount:
    create: true
    annotations:
      iam.gke.io/gcp-service-account: "terrapod-api@my-project.iam.gserviceaccount.com"
  config:
    storage:
      backend: gcs
      gcs:
        bucket: terrapod-storage-my-cluster
        project_id: my-project

runners:
  serviceAccount:
    create: true
    name: "terrapod-runner"
    annotations:
      iam.gke.io/gcp-service-account: "terrapod-runner@my-project.iam.gserviceaccount.com"
```

GKE handles credential projection natively — no additional pod labels or environment variables needed.

---

## Azure Workload Identity Setup

### 1. Create Managed Identities

```bash
# API identity
az identity create --name terrapod-api --resource-group my-rg --location westeurope

# Runner identity
az identity create --name terrapod-runner --resource-group my-rg --location westeurope
```

### 2. Create Federated Credentials

```bash
az identity federated-credential create \
  --name terrapod-api-fed \
  --identity-name terrapod-api \
  --resource-group my-rg \
  --issuer "https://westeurope.oic.prod-aks.azure.com/00000000-0000-0000-0000-000000000000/11111111-1111-1111-1111-111111111111/" \
  --subject "system:serviceaccount:terrapod:terrapod" \
  --audiences "api://AzureADTokenExchange"

az identity federated-credential create \
  --name terrapod-runner-fed \
  --identity-name terrapod-runner \
  --resource-group my-rg \
  --issuer "https://westeurope.oic.prod-aks.azure.com/00000000-0000-0000-0000-000000000000/11111111-1111-1111-1111-111111111111/" \
  --subject "system:serviceaccount:terrapod:terrapod-runner" \
  --audiences "api://AzureADTokenExchange"
```

The `--issuer` is your AKS cluster's OIDC issuer URL (find it with `az aks show -g my-rg -n my-cluster --query oidcIssuerProfile.issuerUrl -o tsv`). The `--subject` must be `system:serviceaccount:<namespace>:<sa-name>` and the `--audiences` must be `api://AzureADTokenExchange`.

### 3. Configure Helm (`values-azure.yaml`)

```yaml
# values-azure.yaml
api:
  serviceAccount:
    create: true
    annotations:
      azure.workload.identity/client-id: "<api-managed-identity-client-id>"
  config:
    storage:
      backend: azure
      azure:
        account_url: "https://mystorageacct.blob.core.windows.net"
        container: terrapod-storage

runners:
  serviceAccount:
    create: true
    name: "terrapod-runner"
    annotations:
      azure.workload.identity/client-id: "<runner-managed-identity-client-id>"
  azureWorkloadIdentity: true  # Adds required pod label to runner Job pods
```

Azure Workload Identity requires a pod label (`azure.workload.identity/use: "true"`) in addition to the SA annotation. Setting `runners.azureWorkloadIdentity: true` in Helm values adds this label to all runner Job pods. (The API pod gets the label from the workload-identity webhook based on the SA annotation.)

---

## Listener ServiceAccount

The listener Deployment also supports SA annotations for cases where the listener itself needs cloud credentials (e.g. for object storage access):

```yaml
listener:
  serviceAccount:
    create: true
    name: "terrapod-listener"
    annotations:
      eks.amazonaws.com/role-arn: "arn:aws:iam::123456789012:role/terrapod-listener"
```

---

## Troubleshooting: top failure modes

Workload identity fails closed and the errors are often opaque. The three most common causes, with how to diagnose each:

### 1. OIDC `sub` / trust-policy mismatch

**Symptom (AWS):** `AccessDenied` / `Not authorized to perform sts:AssumeRoleWithWebIdentity`. **GCP:** `Permission 'iam.serviceAccounts.getAccessToken' denied`. **Azure:** `AADSTS70021: No matching federated identity record found`.

**Cause:** the `sub` the cluster mints (`system:serviceaccount:<namespace>:<sa-name>`) does not exactly equal the trust-policy / binding / federated-credential subject. A namespace typo, the wrong SA name, or pointing at the namespace default SA instead of the named one all produce this.

**How to diagnose:**
- Confirm which SA the pod actually uses:
  `kubectl get pod <pod> -n terrapod -o jsonpath='{.spec.serviceAccountName}'`
- Confirm the SA carries the annotation:
  `kubectl get sa <sa-name> -n terrapod -o yaml`
- Compare character-for-character against the cloud side: the AWS trust-policy `:sub`, the GCP `--member="serviceAccount:<project>.svc.id.goog[<ns>/<sa>]"`, or the Azure `--subject`. The namespace here is `terrapod` and the SA names are `terrapod` (API) / `terrapod-runner` (runner) unless you overrode them.

### 2. Missing or incorrect `audience`

**Symptom:** the assume/exchange is rejected even though the `sub` looks right; AWS reports an audience condition failure, Azure reports the token audience is not accepted.

**Cause:** the projected SA token's `aud` claim doesn't match what the cloud expects. AWS IRSA tokens use audience `sts.amazonaws.com`; Azure Workload Identity uses `api://AzureADTokenExchange`. If you added an explicit `:aud` condition to an AWS trust policy, it must be `sts.amazonaws.com`. On Azure, the federated credential's `--audiences` must be `api://AzureADTokenExchange`.

**How to diagnose:**
- AWS: check the trust policy's `:aud` condition (if present) reads `sts.amazonaws.com`.
- Azure: `az identity federated-credential list --identity-name terrapod-api --resource-group my-rg` and confirm `audiences` is `["api://AzureADTokenExchange"]`.
- Inspect the projected token's audience if needed: `kubectl exec <pod> -n terrapod -- cat /var/run/secrets/...` (path varies by cloud) and decode the JWT's `aud` claim.

### 3. SA ↔ role binding typos

**Symptom:** intermittent or total failure that survives re-deploys; the pod simply never gets credentials and the cloud SDK falls back to "no credentials found".

**Cause:** the annotation value is malformed (wrong account ID, wrong role name, wrong GCP SA email, wrong Azure client-id), or `serviceAccount.create: false` was set while the named SA doesn't actually exist, so the pod runs under the namespace default SA with no annotation at all.

**How to diagnose:**
- Confirm the SA exists and is the one the pod uses (commands in #1).
- AWS: verify the role ARN in the annotation resolves — `aws iam get-role --role-name terrapod-api-my-cluster`.
- GCP: verify the GCP SA email in the annotation exists and the `workloadIdentityUser` binding is present — `gcloud iam service-accounts get-iam-policy terrapod-api@my-project.iam.gserviceaccount.com`.
- Azure: verify the client-id matches the managed identity — `az identity show --name terrapod-api --resource-group my-rg --query clientId -o tsv`.
- If the pod has no cloud env vars at all (`kubectl exec <pod> -- env | grep -i -E 'AWS_|AZURE_|GOOGLE_'` is empty), the workload-identity webhook never matched — the SA annotation is missing or the Azure pod label is absent.

---

## Database authentication

Terrapod's API connects to PostgreSQL using the connection string in the `TERRAPOD_DATABASE_URL` environment variable (sourced from a K8s Secret — see [External secret managers](#external-secret-managers-vault-via-eso--vault-agent)).

The auth mode is selected by `api.config.database.auth_mode`. Besides the default static password, every major cloud's managed-PostgreSQL IAM auth is supported — Terrapod mints a short-lived token **per connection** under the API pod's workload identity (the same IRSA / WIF / WI used for object storage), so there is **no static database password**:

| Mode | What it does | Identity |
|---|---|---|
| `password` (default) | The static password embedded in `TERRAPOD_DATABASE_URL`. Fully supported; recommended to source the Secret from a manager (below). | — |
| `aws_iam` | **AWS RDS IAM auth** — a short-lived (~15-min) IAM token, signed locally per connection. | IRSA |
| `gcp_iam` | **GCP Cloud SQL IAM auth** — the service account's OAuth2 access token as the DB password. | Workload Identity Federation |
| `azure_ad` | **Azure Database for PostgreSQL — Microsoft Entra auth** — an Entra access token as the DB password. | Azure Workload Identity |

In every IAM mode the DB URL carries the **user but no password** (the token is the password), and **TLS is always on**. The credential libraries cache and refresh tokens near expiry; minting is offloaded to a worker thread so it never blocks the API event loop, and a fresh token is supplied per *new* connection. Existing connections persist after a token expires (the token only authenticates the initial handshake).

#### TLS modes (`ssl_mode` + `ssl_root_cert`)

All IAM modes encrypt the connection. `ssl_mode` controls server-certificate verification:

| `ssl_mode` | Behaviour | Needs `ssl_root_cert`? |
|---|---|---|
| `` / `require` (default) | Encrypt, but do **not** verify the server certificate. | No |
| `verify-ca` | Verify the server cert chains to a trusted CA. | Yes (unless the CA is in the system trust store) |
| `verify-full` | `verify-ca` **plus** hostname match — recommended. | Yes (unless system-trusted) |

`verify-ca`/`verify-full` need the provider's CA bundle (e.g. the AWS RDS [`global-bundle.pem`](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html) or the Cloud SQL server CA). A CA bundle is **public, non-secret** material, so the chart takes it as a ConfigMap via the first-class `api.databaseCA` block — paste it inline (the chart creates the ConfigMap) or reference an existing one. The chart mounts it read-only and wires `database.ssl_root_cert` to the mounted path automatically:

```yaml
api:
  databaseCA:
    # Option 1 — paste the CA inline; the chart creates the ConfigMap:
    inline: |
      -----BEGIN CERTIFICATE-----
      ...the RDS / Cloud SQL CA bundle...
      -----END CERTIFICATE-----
    # Option 2 — reference an existing ConfigMap instead of `inline`:
    #   existingConfigMap: rds-ca-bundle
    key: ca.pem            # filename to mount (and the ConfigMap key)
  config:
    database:
      ssl_mode: verify-full   # ssl_root_cert is set for you from api.databaseCA
```

If neither `ssl_root_cert` nor `api.databaseCA` is set, the system CA trust store is used — sufficient for providers whose server cert chains to a public root (e.g. Azure's DigiCert root), but AWS RDS and Cloud SQL use private CAs, so supply the bundle. (Operators with an existing mounting convention can instead set `database.ssl_root_cert` to a path they mount via `api.extraVolumes`; `api.databaseCA` is the recommended path.)

### AWS RDS IAM auth (`auth_mode: aws_iam`)

Setup:

1. **Enable IAM auth on the RDS instance** and create the database role as an IAM-authenticated user:
   ```sql
   CREATE USER terrapod;
   GRANT rds_iam TO terrapod;
   ```
2. **Grant the API's IRSA role `rds-db:connect`** on that DB user (in addition to its object-storage permissions):
   ```json
   {
     "Effect": "Allow",
     "Action": "rds-db:connect",
     "Resource": "arn:aws:rds-db:us-east-1:123456789012:dbuser:db-ABCDEFGHIJKL/terrapod"
   }
   ```
3. **Point the DB URL at the user with no password** (the token is the password) and select the mode. TLS is required for IAM auth and is forced on:
   ```yaml
   api:
     config:
       database:
         auth_mode: aws_iam
         aws_iam_region: ""        # "" = AWS_REGION env (set by IRSA)
         ssl_mode: verify-full     # recommended; needs the RDS CA bundle (see TLS modes above)
         ssl_root_cert: /etc/db-ca/global-bundle.pem
   ```
   ```
   # TERRAPOD_DATABASE_URL — user + host, NO password
   postgresql+asyncpg://terrapod@my-db.abcdef.us-east-1.rds.amazonaws.com:5432/terrapod
   ```

If the API logs `Database auth: cloud IAM (per-connection token)` at startup and a `SELECT 1` succeeds, it's working. A `PAM authentication failed` / `password authentication failed` error usually means the `rds_iam` grant or the `rds-db:connect` IAM policy resource ARN (DBI resource id + user) doesn't match.

### GCP Cloud SQL IAM auth (`auth_mode: gcp_iam`)

On Cloud SQL for PostgreSQL with IAM authentication enabled, Terrapod uses the API service account's OAuth2 access token (scope `https://www.googleapis.com/auth/sqlservice.login`) as the DB password, refreshed per connection via Application Default Credentials.

Setup:

1. **Enable IAM authentication** on the Cloud SQL instance (`cloudsql.iam_authentication = on`) and add the API's GCP service account as an [IAM database user](https://cloud.google.com/sql/docs/postgres/add-manage-iam-users) — the DB user is the SA email **without** the `.gserviceaccount.com` suffix.
2. **Grant the SA** the role `roles/cloudsql.instanceUser` (the `cloudsql.instances.login` permission) plus `roles/cloudsql.client`.
3. **Bind the API's K8s ServiceAccount to that GCP SA via Workload Identity Federation** (the same WIF binding used for GCS — see [GCP](#gcp-workload-identity-federation)).
4. **Configure the mode** (the DB host is the instance's private IP or a Cloud SQL Auth Proxy sidecar; the user is the truncated SA email, no password):
   ```yaml
   api:
     config:
       database:
         auth_mode: gcp_iam
         ssl_mode: verify-ca        # needs the Cloud SQL server CA (see TLS modes above)
         ssl_root_cert: /etc/db-ca/server-ca.pem
   ```
   ```
   # TERRAPOD_DATABASE_URL — IAM DB user (SA email minus the gserviceaccount suffix), NO password
   postgresql+asyncpg://terrapod-api%40my-project.iam@10.20.0.5:5432/terrapod
   ```

### Azure Database for PostgreSQL — Microsoft Entra auth (`auth_mode: azure_ad`)

On Azure Database for PostgreSQL (Flexible Server) with Microsoft Entra authentication, Terrapod mints an Entra access token (scope `https://ossrdbms-aad.database.windows.net/.default`) per connection via `DefaultAzureCredential`, using the pod's Azure Workload Identity.

Setup:

1. **Enable Microsoft Entra authentication** on the Flexible Server and set an Entra admin.
2. **Create a PostgreSQL role for the API's managed identity** — as the Entra admin, run `SELECT * FROM pgaadauth_create_principal('terrapod-api', false, false);` — the DB user is the managed identity's name.
3. **Bind the API's K8s ServiceAccount to the user-assigned managed identity via Azure Workload Identity** (the same binding used for Blob storage — see [Azure](#azure-workload-identity)); the pod needs the `azure.workload.identity/use: "true"` label.
4. **Configure the mode** (the user is the managed identity's name; the token is the password):
   ```yaml
   api:
     config:
       database:
         auth_mode: azure_ad
         ssl_mode: require          # Azure requires TLS
   ```
   ```
   # TERRAPOD_DATABASE_URL — Entra DB user, NO password
   postgresql+asyncpg://terrapod-api@my-server.postgres.database.azure.com:5432/terrapod
   ```

### Recommended pattern for `auth_mode: password`

Use a **strong static database password**, but keep it out of source and rotate it operationally:

1. Generate a strong password and store it in a K8s Secret (the chart reads `TERRAPOD_DATABASE_URL` via `secretKeyRef` when `postgresql.existingSecret` is set).
2. Optionally source that Secret from an external secret manager at deploy time — see [External secret managers](#external-secret-managers-vault-via-eso--vault-agent). External Secrets Operator with a Vault backend can render the DB URL Secret from a Vault path, so the password never lives in your Helm values or Git.
3. Rely on the cloud database's **encryption at rest** (RDS encryption, Cloud SQL encryption, Azure Database encryption) and **network isolation** (private subnets / VPC peering / private endpoints, security groups) so the connection is never exposed publicly.

---

## Redis/Valkey authentication

The API connects to Redis/Valkey using the URL in `TERRAPOD_REDIS_URL` (sourced from a K8s Secret). The auth mode is `api.config.redis.auth_mode`; like the database, each cloud's managed Redis/Valkey supports a passwordless token under the API pod's workload identity (the same IRSA / WIF / WI), so no static Redis auth string is needed:

| Mode | What it does | Identity |
|---|---|---|
| `password` (default) | The static auth string in `TERRAPOD_REDIS_URL`. Fully supported. | — |
| `aws_iam` | **AWS ElastiCache IAM auth** — a SigV4-presigned `connect` token, signed locally per connection. | IRSA |
| `gcp_iam` | **GCP Memorystore IAM auth** — the SA's OAuth2 access token (`cloud-platform` scope). | WIF |
| `azure_ad` | **Azure Cache for Redis — Microsoft Entra auth** — an Entra token (`redis.azure.com` scope). | Azure WI |

A fresh token is minted per connection via a redis-py credential provider (offloaded to a thread, so the event loop is never blocked), and the token libraries cache + refresh near expiry. **TLS is required** for IAM Redis auth — use a `rediss://` URL. The URL's userinfo is ignored in IAM mode (the token replaces it), so the `TERRAPOD_REDIS_URL` Secret just needs the host/port (and `rediss://`).

### AWS ElastiCache IAM auth (`auth_mode: aws_iam`)

1. **Enable IAM auth on the cache** (Redis OSS 7+ / Valkey, incl. Serverless) and create an [ElastiCache User](https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/auth-iam.html) with **Authentication Mode = IAM**, in a User Group attached to the cache. The user name must match the IAM-authenticated identity.
2. **Grant the API's IRSA role `elasticache:Connect`** on the cache + user ARNs (in addition to its other permissions).
3. **Configure the mode** (the username is the ElastiCache User; `aws_cache_name` is the **cache identifier** — the replication-group id or serverless cache name, *not* the endpoint host — used for signing):
   ```yaml
   api:
     config:
       redis:
         auth_mode: aws_iam
         username: terrapod
         aws_cache_name: terrapod-cache   # replication-group / serverless cache id
         aws_iam_region: ""               # "" = AWS_REGION env (set by IRSA)
   ```
   ```
   # TERRAPOD_REDIS_URL — TLS, host + port, NO auth string
   rediss://terrapod-cache-abc123.serverless.us-east-1.cache.amazonaws.com:6379
   ```

### GCP Memorystore IAM auth (`auth_mode: gcp_iam`)

On Memorystore for Valkey / Redis Cluster with IAM authentication enabled, Terrapod uses the API service account's OAuth2 access token (`cloud-platform` scope) as the Redis password.

1. **Enable IAM authentication** on the instance and grant the API's GCP service account `roles/redis.dbConnectionUser`.
2. **Bind the API's K8s ServiceAccount to that GCP SA via Workload Identity Federation** (the same binding used for GCS).
3. **Configure the mode** (the username is the IAM user):
   ```yaml
   api:
     config:
       redis:
         auth_mode: gcp_iam
         username: terrapod-api@my-project.iam.gserviceaccount.com
   ```

### Azure Cache for Redis — Microsoft Entra auth (`auth_mode: azure_ad`)

On Azure Cache for Redis with Microsoft Entra authentication, Terrapod mints an Entra access token (`https://redis.azure.com/.default`) via `DefaultAzureCredential`, using the pod's Azure Workload Identity.

1. **Enable Entra authentication** on the cache and add the API's managed identity as a Redis user with a data-access policy.
2. **Bind the API's K8s ServiceAccount to the user-assigned managed identity via Azure Workload Identity** (the same binding used for Blob storage); the pod needs the `azure.workload.identity/use: "true"` label.
3. **Configure the mode** (the username is the managed identity's **object id**):
   ```yaml
   api:
     config:
       redis:
         auth_mode: azure_ad
         username: 00000000-0000-0000-0000-000000000000   # managed-identity object id
   ```

---

## External secret managers (Vault via ESO / Vault Agent)

Terrapod has **no built-in Vault integration** — that is deliberately out of scope. The supported pattern is an **external** secret manager (e.g. HashiCorp Vault) feeding Terrapod's existing Kubernetes Secrets, using either:

- **External Secrets Operator (ESO)** with the Vault backend — ESO syncs a Vault path into a native K8s Secret, which Terrapod then reads via `secretKeyRef`; or
- **Vault Agent injector** — Vault Agent renders secrets into the pod, which you point the chart's `existingSecret` references at.

### Platform secrets Vault can populate

These platform secrets are read from K8s Secrets / env and can therefore be sourced from Vault via ESO or the Vault Agent injector:

| Secret | How Terrapod reads it | Helm key |
|---|---|---|
| **Database URL** | `TERRAPOD_DATABASE_URL` via `secretKeyRef` | `postgresql.existingSecret` / `postgresql.existingSecretKey` (default key `url`) |
| **Redis URL** | `TERRAPOD_REDIS_URL` via `secretKeyRef` | `redis.existingSecret` / `redis.existingSecretKey` (default key `url`) |
| **OIDC client secrets** | `TERRAPOD_<NAME>_CLIENT_SECRET` via `secretKeyRef` | `existingSecret` / `existingSecretKey` per OIDC provider entry (default key `client_secret`) |
| **API token signing key** | `TERRAPOD_TOKEN_SIGNING_KEY` via `secretKeyRef` | `api.tokenSigningKey.existingSecret` / `existingSecretKey` (default key `token_signing_key`) |
| **GitHub webhook secret** | `TERRAPOD_GITHUB_WEBHOOK_SECRET` via `secretKeyRef` | `api.config.vcs.github.existingSecret` / `existingSecretKey` (default key `webhook_secret`) |

Example ESO `ExternalSecret` rendering the DB URL Secret that the chart then consumes:

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: terrapod-postgresql
  namespace: terrapod
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend       # a configured SecretStore/ClusterSecretStore
    kind: ClusterSecretStore
  target:
    name: terrapod-postgresql # the K8s Secret the chart points at
  data:
    - secretKey: url          # becomes key `url` in the Secret
      remoteRef:
        key: secret/data/terrapod/postgresql
        property: url
```

```yaml
# values.yaml — point the chart at the ESO-rendered Secret
postgresql:
  existingSecret: terrapod-postgresql
  existingSecretKey: url
```

> **Not Vault-sourceable:** the GitHub App private key lives on the `VCSConnection` database record (created through the API/UI), **not** in a K8s Secret, so Vault/ESO cannot populate it. Protect it via the database's encryption-at-rest instead.

### Vault *dynamic* secrets for runner cloud creds is not wired up today (honest gap)

A common Vault use-case is **dynamic secrets** — a runner fetching short-lived, per-run cloud credentials with something like `vault read aws/creds/my-role` just before `init`. Terrapod's runner does have a setup-script hook (`TP_SETUP_SCRIPT` → an early `/bin/sh -c` step before `init`) that *could* run such a command, **but it is not wired to any configuration surface**: there is no Helm value, no workspace field, and nothing injects `TP_SETUP_SCRIPT` onto the runner Job. So there is **no turnkey way today** to have a run fetch Vault dynamic secrets.

**The supported way for runs to reach cloud APIs is workload identity** (IRSA/WIF/WI on the runner SA), documented above. A configurable runner setup script — the surface that would enable Vault dynamic secrets and other per-run credential fetching — is future work. This section will be updated if and when that configuration surface ships.

---

## See Also

- [Deployment](deployment.md) — Helm chart configuration
- [Security Hardening](security-hardening.md) — TLS, secrets management, network policies
- [Architecture](architecture.md) — runner infrastructure and job template
- [API Reference](api-reference.md) — agent pool API
- [ACK IAM Controller](https://aws-controllers-k8s.github.io/community/reference/iam/v1alpha1/role/) — managing IAM resources as Kubernetes objects
- [External Secrets Operator](https://external-secrets.io/) — syncing Vault (and other) secrets into Kubernetes
