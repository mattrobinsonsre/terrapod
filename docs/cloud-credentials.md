# Cloud Credentials

Terrapod supports dynamic cloud provider credentials via Kubernetes workload identity. Runner Jobs authenticate with AWS, GCP, or Azure using ServiceAccount annotations — no static credentials are stored in Terrapod.

---

## How It Works

Each cloud provider has a Kubernetes integration that projects short-lived credentials into pods based on ServiceAccount annotations:

| Provider | Mechanism | SA Annotation |
|---|---|---|
| **AWS** | IRSA (IAM Roles for Service Accounts) | `eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/...` |
| **GCP** | Workload Identity Federation | `iam.gke.io/gcp-service-account: ...@project.iam.gserviceaccount.com` |
| **Azure** | Workload Identity | `azure.workload.identity/client-id: <managed-identity-client-id>` |

When a runner Job starts, the cloud provider's mutating admission webhook injects the necessary environment variables and token volumes. Terraform providers (aws, google, azurerm) pick up these credentials automatically.

---

## ServiceAccount Configuration

Runner Jobs use the ServiceAccount configured in Helm values:

| Priority | Source | Configured Via |
|---|---|---|
| 1 | **Global runner SA** | `runners.serviceAccount.name` in Helm values |
| 2 | **K8s default SA** | Implicit namespace default |

For multi-cloud or multi-account setups, deploy separate listener Deployments (agent pools) in different clusters or namespaces, each with their own Helm-configured ServiceAccount.

---

## AWS IRSA Setup

### 1. Configure OIDC Provider

Your EKS cluster must have an IAM OIDC provider configured. This is typically set up during cluster creation.

### 2. Create IAM Role

Create an IAM role with a trust policy that allows the OIDC provider and the Terrapod runner ServiceAccount:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B71EXAMPLE"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B71EXAMPLE:sub": "system:serviceaccount:terrapod:terrapod-runner"
        }
      }
    }
  ]
}
```

### 3. Configure Helm

```yaml
runners:
  serviceAccount:
    create: true
    name: "terrapod-runner"
    annotations:
      eks.amazonaws.com/role-arn: "arn:aws:iam::123456789012:role/terrapod-runner"
```

The EKS mutating webhook automatically injects `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE` environment variables into runner pods. No pod labels required.

---

## GCP Workload Identity Federation Setup

### 1. Create GCP Service Account

```bash
gcloud iam service-accounts create terrapod-runner \
  --project=my-project
```

### 2. Grant Permissions

```bash
gcloud projects add-iam-policy-binding my-project \
  --member="serviceAccount:terrapod-runner@my-project.iam.gserviceaccount.com" \
  --role="roles/editor"
```

### 3. Bind K8s SA to GCP SA

```bash
gcloud iam service-accounts add-iam-policy-binding \
  terrapod-runner@my-project.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="serviceAccount:my-project.svc.id.goog[terrapod/terrapod-runner]"
```

### 4. Configure Helm

```yaml
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

### 1. Create Managed Identity

```bash
az identity create \
  --name terrapod-runner \
  --resource-group my-rg \
  --location westeurope
```

### 2. Create Federated Credential

```bash
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
runners:
  serviceAccount:
    create: true
    name: "terrapod-runner"
    annotations:
      azure.workload.identity/client-id: "<managed-identity-client-id>"
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
