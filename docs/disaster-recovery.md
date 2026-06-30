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
  workspace_id: 7fb95e74-6828-4673-a4bd-3d074a77bcb7
  state_key: state/7fb95e74-6828-4673-a4bd-3d074a77bcb7/9b2c3d4e-5f6a-7890-bcde-f01234567890.tfstate
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

## Routine Backup & Restore

Break-glass recovery above operates on a **single** workspace's state from
object storage. For protecting the whole platform, Terrapod has exactly two
stateful components — back up both:

| Component | Holds | Back up with |
|---|---|---|
| **PostgreSQL** | Workspaces, variables (incl. sensitive), runs, configuration-version metadata, registry metadata, roles/assignments, VCS connections, the CA keypair, audit log | Your database's native backup (RDS automated/manual snapshots, Azure Database backups, `pg_dump`, or a continuous-archiving tool like WAL-G/pgBackRest) |
| **Object storage** | State versions, configuration-version tarballs, plan/apply logs, registry module + provider artifacts, the state index | The cloud provider's bucket-level protection (S3 Versioning + cross-region replication, Azure Blob soft-delete + versioning, GCS Object Versioning), or a periodic bucket sync to a separate account |

Redis holds only ephemeral data (sessions, listener heartbeats, the scheduler's
distributed locks, SSE channels) — it does **not** need backing up; a fresh
Redis is fine after a restore.

**Restore order:** restore PostgreSQL first, then point the new deployment at
the restored object-storage bucket via Helm values. Because every artifact in
object storage is addressed by the UUIDs recorded in PostgreSQL, a
point-in-time PostgreSQL snapshot paired with a same-or-later object-storage
state is self-consistent (extra orphaned objects are harmless). Keep the two
backups loosely time-aligned to minimise orphans. The CA keypair lives in
PostgreSQL, so a DB restore re-establishes listener trust without re-issuing
certificates.

### Shipped backup automation (the baseline floor)

If you don't already run RDS snapshots / WAL-G / pgBackRest, the chart ships an
**optional, off-by-default** logical-backup CronJob. A standard deployment
already has both halves it needs — a Postgres URL and an object-storage backend
— so enabling it needs no new infrastructure or credentials:

```yaml
backup:
  enabled: true
  schedule: "0 2 * * *"     # daily at 02:00 (cluster TZ)
  prefix: "backups/"        # written under this key in the app object store
  retention:
    keep: 14                # keep the 14 most-recent dumps (0 = keep all)
    days: 30                # and/or delete dumps older than 30 days (0 = off)
```

The CronJob runs `pg_dump` (custom format) and streams the dump to
`<prefix><timestamp>.dump` in the configured object store, inheriting the
bucket's at-rest encryption and the API ServiceAccount's cloud workload
identity. It works across S3 / Azure / GCS / filesystem and across
static-password **and** cloud-IAM database auth (it mints the same short-lived
token the API uses).

This is a **logical** dump: RPO is the dump interval, not point-in-time. Treat
it as the floor — for tighter RPO keep using RDS snapshots / WAL-G / pgBackRest
(set `backup.enabled: false` and rely on those instead).

**Keep the backup out of the app data's blast radius.** Rather than dual-writing
app-side, enable object-store **cross-region/account replication** on the bucket
so the dumps (and all state) land in a second location automatically — see the
per-backend toggles below.

### Object-storage protection per backend

Object storage holds state, config tarballs, logs, registry artifacts **and**
(when enabled) the DB dumps above. Turn on the provider's native protections:

| Backend | Versioning (undo overwrite/delete) | Off-site copy |
|---|---|---|
| **AWS S3** | `aws s3api put-bucket-versioning --versioning-configuration Status=Enabled` | S3 Cross-Region Replication (CRR) to a bucket in another account/region |
| **Azure Blob** | enable Blob versioning + soft-delete on the storage account | Object replication to a second account |
| **GCS** | `gcloud storage buckets update gs://BUCKET --versioning` | Bucket cross-region/dual-region or `gcloud storage rsync` to a second bucket |

Versioning protects against accidental overwrite/delete; replication protects
against losing the whole bucket/region. Both are operator-managed (Terrapod
never needs delete-then-recreate semantics on these objects).

### Restore-verification drill (DR drill)

A tested restore beats a documented one. The chart ships an optional
**restore-verify** CronJob that restores the latest backup into a **throwaway
sidecar Postgres** (never the live database) and asserts core invariants — the
schema + CA load, workspaces resolve, and a state object downloads from the
store. It exits non-zero on any failure, so it's a real green check:

```yaml
backup:
  enabled: true
  restoreVerify:
    enabled: true
    schedule: "0 4 * * 0"   # weekly DR drill, Sundays 04:00
```

Run it **on demand** any time:

```bash
kubectl create job --from=cronjob/<release>-terrapod-restore-verify drill-now -n <ns>
kubectl logs -f job/drill-now -n <ns>
```

A failing drill is paged via the shipped alert path (the CronJob's Job failure
surfaces through kube-state-metrics / your Job-failure alerts). See the
[restore-failed runbook](runbooks.md#dr-restore-drill-failed).

### Full restore (production)

1. **Restore PostgreSQL first.** From an RDS/Azure snapshot, or from a logical
   dump: `pg_restore --clean --if-exists -d "$DATABASE_URL" <dump>` (the dump
   the CronJob wrote, fetched from `backups/` in object storage).
2. **Point the new deployment at the restored object-storage bucket** via Helm
   values (same bucket, or the replication target).
3. **Bring up Terrapod.** The CA keypair came back with the DB, so listeners
   re-establish trust on reconnect; Redis starts fresh. Confirm with the
   on-demand drill above before declaring recovery complete.
