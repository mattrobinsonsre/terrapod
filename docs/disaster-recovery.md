# Disaster Recovery: Break-Glass State Recovery

When Terrapod is unavailable (database down, API unreachable, cluster failure), operators may need to recover Terraform state files directly from object storage to perform emergency infrastructure changes.

## Prerequisites

- Access to the object storage backend (S3, Azure Blob, or GCS)
- The Terraform/OpenTofu code for the workspace (from your VCS provider)
- Cloud credentials for the target infrastructure

## State Index

Terrapod maintains a `state/index.yaml` file in object storage that maps workspace names to their latest state file paths. This index is updated on every state upload, workspace rename, and workspace deletion.

Example index contents:

```yaml
my-vpc:
  workspace_id: 3fa85f64-5717-4562-b3fc-2c963f66afa6
  state_key: state/3fa85f64-5717-4562-b3fc-2c963f66afa6/8a1b2c3d-4e5f-6789-abcd-ef0123456789.tfstate
  serial: 42
  updated_at: '2026-03-25T10:30:00Z'
production-rds:
  workspace_id: 7fb95e74-6828-4673-c4gd-3d074g77bgb7
  state_key: state/7fb95e74-6828-4673-c4gd-3d074g77bgb7/9b2c3d4e-5f6a-7890-bcde-f01234567890.tfstate
  serial: 15
  updated_at: '2026-03-24T14:00:00Z'
```

## Step 1: Download the State Index

### AWS S3

```bash
aws s3 cp s3://<bucket>/state/index.yaml .
```

### Azure Blob Storage

```bash
az storage blob download \
  --container-name <container> \
  --name state/index.yaml \
  --file index.yaml \
  --account-name <account>
```

### Google Cloud Storage

```bash
gsutil cp gs://<bucket>/state/index.yaml .
```

### Filesystem (local dev)

```bash
cp <storage-path>/state/index.yaml .
```

## Step 2: Find the State File Path

Open `index.yaml` and locate the workspace by name. The `state_key` field contains the path to the latest state file in object storage.

```bash
# Quick lookup with yq
yq '.["my-vpc"].state_key' index.yaml
```

## Step 3: Download the State File

Using the `state_key` from the index:

### AWS S3

```bash
aws s3 cp s3://<bucket>/<state_key> terraform.tfstate
```

### Azure Blob Storage

```bash
az storage blob download \
  --container-name <container> \
  --name <state_key> \
  --file terraform.tfstate \
  --account-name <account>
```

### Google Cloud Storage

```bash
gsutil cp gs://<bucket>/<state_key> terraform.tfstate
```

## Step 4: Clone the Terraform Code

```bash
git clone <repo-url>
cd <repo-directory>
# Check out the branch/commit that matches the state
```

If the workspace uses a working directory, `cd` into it.

## Step 5: Remove the Cloud Backend Block

The Terraform code will contain a `cloud {}` or `backend "..." {}` block pointing at Terrapod. Remove or comment it out — you need to run with a local backend during recovery.

```bash
# Find and edit the file containing the cloud block
grep -rl 'cloud {' *.tf
# Remove or comment out the cloud {} block
```

## Step 6: Initialize and Apply

```bash
# Initialize with local backend, using the downloaded state
terraform init -migrate-state

# Or if init doesn't pick up the state file automatically:
terraform init
cp terraform.tfstate .

# Review the plan
terraform plan

# Apply if changes are needed
terraform apply
```

## Step 7: Restore State to Terrapod

Once Terrapod is back online, migrate the state back:

1. Restore the `cloud {}` block in your Terraform configuration
2. Run `terraform init -migrate-state` to push state back to Terrapod
3. Verify the state serial incremented correctly in the Terrapod UI

## Important Notes

### Variables

Terrapod-managed variables (both Terraform and environment variables) are stored in PostgreSQL, not in object storage. During break-glass recovery:

- **Terraform variables**: Set them via `terraform.tfvars` or `-var` flags
- **Environment variables**: Export them in your shell before running `terraform plan/apply`
- If you have a backup of your Terrapod database, variables can be extracted from the `variables` table

### Cloud Credentials (Workload Identity)

If your workspaces use dynamic provider credentials via Kubernetes workload identity (AWS IRSA, GCP WIF, Azure WI), those credentials are tied to the runner's ServiceAccount. During break-glass recovery, you will need to authenticate to your cloud provider using alternative credentials (e.g., CLI profiles, environment variables, or temporary credentials).

### State Locking

During break-glass recovery with a local backend, there is no state locking. Coordinate with your team to ensure only one person modifies the state at a time.

### Index Accuracy

The state index is best-effort — it is updated on every state upload but failures are swallowed to avoid blocking state operations. In rare cases, the index may be slightly out of date. If you cannot find a workspace in the index, state files are stored at `state/<workspace_uuid>/<state_version_uuid>.tfstate` — list the workspace's directory to find the latest file by timestamp.
