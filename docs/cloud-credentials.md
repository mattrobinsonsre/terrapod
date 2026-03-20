# Cloud Credentials

Terrapod supports dynamic cloud provider credentials via Kubernetes workload identity. Both the API server and runner Jobs authenticate with AWS, GCP, or Azure using ServiceAccount annotations — no static credentials are stored in Terrapod.

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

---

## How It Works

Each cloud provider has a Kubernetes integration that projects short-lived credentials into pods based on ServiceAccount annotations:

| Provider | Mechanism | SA Annotation |
|---|---|---|
| **AWS** | IRSA (IAM Roles for Service Accounts) | `eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/...` |
| **GCP** | Workload Identity Federation | `iam.gke.io/gcp-service-account: ...@project.iam.gserviceaccount.com` |
| **Azure** | Workload Identity | `azure.workload.identity/client-id: <managed-identity-client-id>` |

When a pod starts, the cloud provider's mutating admission webhook injects the necessary environment variables and token volumes. Terraform providers (aws, google, azurerm) pick up these credentials automatically.

---

## ServiceAccount Configuration

### API ServiceAccount

The API server uses its own ServiceAccount for object storage access:

```yaml
api:
  serviceAccount:
    create: true
    annotations:
      eks.amazonaws.com/role-arn: "arn:aws:iam::123456789012:role/terrapod-api-mycluster"
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
      eks.amazonaws.com/role-arn: "arn:aws:iam::123456789012:role/terrapod-runner-mycluster"
```

The runner SA is only created when `listener.enabled: true` (i.e. on clusters that actually run Jobs).

For multi-cloud or multi-account setups, deploy separate listener Deployments (agent pools) in different clusters or namespaces, each with their own Helm-configured ServiceAccount.

---

## AWS IRSA Setup

### 1. Configure OIDC Provider

Your EKS cluster must have an IAM OIDC provider configured. This is typically set up during cluster creation.

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
          "oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLE:sub": "system:serviceaccount:terrapod:terrapod"
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
          "oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLE:sub": "system:serviceaccount:terrapod:terrapod-runner"
        }
      }
    }
  ]
}
```

### 3. Configure Helm

```yaml
# API SA — object storage access
api:
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: "arn:aws:iam::123456789012:role/terrapod-api-mycluster"

# Runner SA — Terraform workload permissions
runners:
  serviceAccount:
    create: true
    name: "terrapod-runner"
    annotations:
      eks.amazonaws.com/role-arn: "arn:aws:iam::123456789012:role/terrapod-runner-mycluster"
```

The EKS mutating webhook automatically injects `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` environment variables into pods. No pod labels required.

### Managing IAM with AWS Controllers for Kubernetes (ACK)

If your cluster runs the [ACK IAM controller](https://aws-controllers-k8s.github.io/community/docs/community/services/#iam), you can declare IAM Policies and Roles as Kubernetes resources. This keeps IAM definitions version-controlled alongside your Helm chart:

```yaml
apiVersion: iam.services.k8s.aws/v1alpha1
kind: Policy
metadata:
  name: terrapod-api-mycluster
  annotations:
    services.k8s.aws/adoption-policy: "adopt-or-create"
    services.k8s.aws/region: "eu-west-1"
spec:
  name: terrapod-api-mycluster
  description: "Terrapod API — S3 object storage access"
  policyDocument: |
    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetBucketLocation"],
          "Resource": ["arn:aws:s3:::terrapod-storage-mycluster", "arn:aws:s3:::terrapod-storage-mycluster/*"]
        }
      ]
    }
---
apiVersion: iam.services.k8s.aws/v1alpha1
kind: Role
metadata:
  name: terrapod-api-mycluster
  annotations:
    services.k8s.aws/adoption-policy: "adopt-or-create"
    services.k8s.aws/region: "eu-west-1"
spec:
  name: terrapod-api-mycluster
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
    - "arn:aws:iam::123456789012:policy/terrapod-api-mycluster"
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

### 4. Configure Helm

```yaml
api:
  serviceAccount:
    annotations:
      iam.gke.io/gcp-service-account: "terrapod-api@my-project.iam.gserviceaccount.com"

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
  --issuer "https://oidc.prod-aks.azure.com/tenant-id/" \
  --subject "system:serviceaccount:terrapod:terrapod" \
  --audiences "api://AzureADTokenExchange"

az identity federated-credential create \
  --name terrapod-runner-fed \
  --identity-name terrapod-runner \
  --resource-group my-rg \
  --issuer "https://oidc.prod-aks.azure.com/tenant-id/" \
  --subject "system:serviceaccount:terrapod:terrapod-runner" \
  --audiences "api://AzureADTokenExchange"
```

### 3. Configure Helm

```yaml
api:
  serviceAccount:
    annotations:
      azure.workload.identity/client-id: "<api-managed-identity-client-id>"

runners:
  serviceAccount:
    create: true
    name: "terrapod-runner"
    annotations:
      azure.workload.identity/client-id: "<runner-managed-identity-client-id>"
  azureWorkloadIdentity: true  # Adds required pod label
```

Azure Workload Identity requires a pod label (`azure.workload.identity/use: "true"`) in addition to the SA annotation. Setting `runners.azureWorkloadIdentity: true` in Helm values adds this label to all runner Job pods.

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

## See Also

- [Deployment](deployment.md) — Helm chart configuration
- [Architecture](architecture.md) — runner infrastructure and job template
- [API Reference](api-reference.md) — agent pool API
- [ACK IAM Controller](https://aws-controllers-k8s.github.io/community/reference/iam/v1alpha1/role/) — managing IAM resources as Kubernetes objects
