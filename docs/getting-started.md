# Getting Started

This guide deploys Terrapod onto a Kubernetes cluster with the Helm chart, then creates your first workspace and runs your first plan and apply against it.

> Terrapod runs **only on Kubernetes** — the runner uses the Jobs API to schedule plan/apply executions. You don't need a managed cloud cluster; a single-node [k3s](https://k3s.io/) VM works. If you instead want to run Terrapod **from source** for development, see [Local Development](local-development.md).

> **OpenTofu is the recommended execution backend.** Terrapod supports both [OpenTofu](https://opentofu.org/) (`tofu`) and Terraform (`terraform`) as execution backends. OpenTofu is recommended because it is open-source under the MPL-2.0 license. All CLI examples in this guide use `tofu`, but `terraform` commands work identically.

---

## Just kicking the tyres? One-command evaluation

To try Terrapod end-to-end on your laptop without provisioning anything, skip
the rest of this guide and run the evaluation quickstart. It spins up a
throwaway [kind](https://kind.sigs.k8s.io/) or [k3d](https://k3d.io/) cluster
and installs a **complete, self-contained stack** — in-cluster PostgreSQL +
Redis (deployed by the chart), filesystem storage, and a local admin — with no
cloud account and no external dependencies:

```sh
make eval          # create a local cluster + install Terrapod, then port-forward
# → open http://localhost:8080  (login: admin / terrapod)
make eval-down     # delete it all
```

Prerequisites: Docker, `kubectl`, `helm`, and either `kind` or `k3d`. This is an
**evaluation** profile (single-replica datastores, a known password, no
HA/backups) — for a real deployment, continue below. Agent execution (server-side
plan/apply) is off in the eval to keep it small; create an agent pool and set
`listener.enabled=true` to try it.

---

## Prerequisites

| Need | Notes |
|---|---|
| A Kubernetes cluster (1.27+) | Any conformant cluster. No cluster? Spin up [k3s](https://k3s.io/) on a single VM (below). |
| Helm 3.x | `brew install helm` |
| PostgreSQL 14+ and Redis 7+ | **External** — the chart does not bundle them. Provide a connection URL for each (a managed service, or run them on the cluster/VM). See [Deployment → Database Setup](deployment.md#database-setup). |
| Storage | Defaults to a PVC-backed filesystem (no extra setup). For S3/Azure/GCS see [Deployment → Storage Backend Setup](deployment.md#storage-backend-setup). |
| `tofu` (recommended) or `terraform` | Infrastructure CLI — [opentofu.org](https://opentofu.org/) or [terraform.io](https://www.terraform.io/). |

### Don't have a cluster? Use k3s

[k3s](https://k3s.io/) is a fully conformant single-binary Kubernetes that runs on one VM (~512 MB RAM) and ships with an ingress controller (Traefik) and local-path storage:

```zsh
curl -sfL https://get.k3s.io | sh -          # single-node cluster
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
```

Run PostgreSQL and Redis as managed services or on a separate VM so your data survives cluster recreation.

---

## Deploy Terrapod

Terrapod is published to GitHub Container Registry as an OCI Helm chart at `oci://ghcr.io/mattrobinsonsre/terrapod`.

Pick a hostname and install:

```zsh
export TERRAPOD_HOST=terrapod.example.com

helm install terrapod oci://ghcr.io/mattrobinsonsre/terrapod \
  --namespace terrapod --create-namespace \
  --set ingress.enabled=true \
  --set ingress.hostname="$TERRAPOD_HOST" \
  --set ingress.className=traefik \
  --set postgresql.url="postgresql+asyncpg://terrapod:PASSWORD@PGHOST:5432/terrapod" \
  --set redis.url="redis://REDISHOST:6379" \
  --set bootstrap.adminEmail="admin@example.com" \
  --set bootstrap.adminPassword="change-me-now"
```

What the defaults give you: filesystem storage on a PVC, local password auth enabled, a pre-install migrations job, and a post-install bootstrap job that creates the admin user from `bootstrap.adminEmail` / `bootstrap.adminPassword`. Set `ingress.className` to your cluster's ingress controller (`traefik` on k3s, often `nginx` on managed clusters). Pin a version with `--version X.Y.Z`.

> For production, put the admin password in a Kubernetes Secret (`bootstrap.existingSecret`) rather than on the command line, terminate TLS at the ingress (e.g. cert-manager), and set `api.config.external_url` so outbound links (VCS status, notifications) are correct. The full value reference — storage backends, external DB, SSO, scaling, TLS, runners — is in [Deployment](deployment.md), and [Production Checklist](production-checklist.md) covers hardening.

Watch it come up:

```zsh
kubectl -n terrapod get pods -w
```

### Access

Point your hostname's DNS at the ingress controller's external IP (on k3s, the VM's IP — Traefik listens on ports 80/443). For a quick local test you can add a hosts entry instead:

```zsh
sudo sh -c 'echo "<INGRESS_IP> terrapod.example.com" >> /etc/hosts'
```

Then open `https://terrapod.example.com` and log in with the bootstrap admin email + password.

> TLS: the ingress expects a certificate by default (`ingress.tls=true`). For a quick HTTP-only evaluation, add `--set ingress.tls=false`; for real TLS see [Deployment → Ingress](deployment.md#ingress).
>
> No DNS available at all? Port-forward the web service for a quick look: `kubectl -n terrapod port-forward svc/terrapod-web 8080:3000`, then open `http://localhost:8080`.

### Get an API token

In the web UI, go to **Settings → API Tokens**, create a token, and copy the value (shown once):

```zsh
export TERRAPOD_TOKEN="<your-token-value>"
```

Or let the CLI mint one for you via the browser login flow:

```zsh
tofu login terrapod.example.com
```

---

## Creating Your First Workspace

### Via the API

```zsh
curl -s -X POST https://terrapod.example.com/api/v2/organizations/default/workspaces \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "workspaces",
      "attributes": {
        "name": "demo",
        "auto-apply": true
      }
    }
  }' | jq .
```

Note the workspace ID from the response (e.g., `ws-abc123...`).

### Via the Web UI

1. Navigate to **Workspaces**
2. Click **New Workspace**
3. Enter a name (e.g., `demo`)
4. Optionally enable auto-apply
5. Click **Create**

![Workspaces](images/workspaces.png)

---

## Configuring the CLI

Create a configuration that uses Terrapod as its backend:

```hcl
# main.tf
terraform {
  cloud {
    hostname     = "terrapod.example.com"
    organization = "default"

    workspaces {
      name = "demo"
    }
  }
}

# A simple example resource
resource "null_resource" "hello" {
  triggers = {
    timestamp = timestamp()
  }

  provisioner "local-exec" {
    command = "echo 'Hello from Terrapod!'"
  }
}
```

> Note: The `terraform {}` block syntax is used by both OpenTofu and Terraform. No changes are needed when switching between them.

> **Do not set `project = "..."` in the cloud block.** Terrapod is single-organization and has no project concept — workspaces live directly under the organization. Setting `project` causes `tofu init` to fail with a `422 Projects are not supported` error from the API. Omit the argument. (Single-org is a deliberate design choice aligned with HashiCorp's current direction — see [Why a single organization](architecture.md#why-a-single-organization).)
>
> If you used TFC projects to scope RBAC (e.g. "team X has write on project Y"), Terrapod's [label-based RBAC](rbac.md) covers the same use case and more flexibly: roles match workspaces by label allow/deny rules (e.g. `team: payments` + `env: prod` → `write`), so a workspace can belong to any combination of dimensions instead of a single project. The same labels also drive **filtering in the web UI** — workspace lists, registry modules, and other resources can be filtered by any label key/value, so labels double as the navigation hierarchy projects provided in TFC. See `docs/rbac.md` for examples.

If you'd rather select a workspace at run time (typical when a single repo backs several environments) replace `name` with `tags`. Tags are matched against the workspace's labels — see the [RBAC docs](rbac.md#labels-are-also-tags) for details.

```hcl
workspaces {
  tags = ["network"]                 # any workspace with the label key "network"
  # or, for an exact key:value match:
  tags = ["repo:aws-infra"]          # workspaces whose `repo` label is `aws-infra`
}
```

> OpenTofu only accepts the **set-of-strings** `tags` form, so use the `key:value` string above for an exact match. The Terraform 1.10+ **map** form (`tags = { repo = "aws-infra" }`) is rejected by `tofu` at config validation with `set of string required`.

Then pick a specific workspace per invocation:

```zsh
TF_WORKSPACE=network-staging tofu plan
# or
tofu workspace select network-staging
```

If you have not already run `tofu login`:

```zsh
tofu login terrapod.example.com
```

Then initialize:

```zsh
tofu init
```

You should see output confirming that OpenTofu is using Terrapod as its backend:

```
Initializing Terraform Cloud...
...
Terraform Cloud has been successfully initialized!
```

---

## Running Your First Plan

### Local Execution Mode

In local execution mode, `tofu` runs on your machine and pushes state to Terrapod:

```zsh
tofu plan
```

The plan output appears in your terminal as usual. State locking is handled by Terrapod (lock/unlock API calls).

```zsh
tofu apply
```

State is uploaded to Terrapod after a successful apply. You can view state versions in the web UI under the workspace's **State** tab.

### Agent Execution Mode

For agent execution, the runner listener creates a K8s Job that runs tofu (or terraform) on the server via an agent pool.

Agent workspaces support two workflows depending on whether VCS is connected:

**VCS-connected workspaces** — VCS is the source of truth. CLI can only plan; applies must come through VCS.

**Non-VCS workspaces (CLI-driven)** — the CLI is the source of truth. `tofu plan` and `tofu apply` both work, with the apply phase running on the server after plan confirmation.

| Source | VCS-Connected | Non-VCS (CLI-driven) |
|---|---|---|
| `tofu plan` (CLI) | Plan-only on server | Plan-only on server |
| `tofu apply` (CLI) | Blocked (use VCS) | Plan + confirm + apply on server |
| VCS push to tracked branch | Full plan + apply | N/A |
| VCS pull request / merge request | Speculative plan-only | N/A |

1. Set the workspace execution mode to `agent`:

```zsh
curl -X PATCH https://terrapod.example.com/api/v2/workspaces/ws-{id} \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "workspaces",
      "attributes": {
        "execution-mode": "agent"
      }
    }
  }'
```

2. Create a configuration version and upload your code:

```zsh
# Create configuration version
CV_RESPONSE=$(curl -s -X POST https://terrapod.example.com/api/v2/workspaces/ws-{id}/configuration-versions \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{"data": {"type": "configuration-versions", "attributes": {"auto-queue-runs": true}}}')

# Extract upload URL
UPLOAD_URL=$(echo "$CV_RESPONSE" | jq -r '.data.attributes."upload-url"')

# Create tarball and upload
tar -czf config.tar.gz -C /path/to/terraform/dir .
curl -X PUT "$UPLOAD_URL" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @config.tar.gz
```

3. A run is automatically queued (plan-only). Monitor it:

```zsh
curl -s https://terrapod.example.com/api/v2/workspaces/ws-{id}/runs \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" | jq '.data[0].attributes.status'
```

---

## Setting Up Workspace Variables

Variables can be set per-workspace via the API or web UI.

![Workspace Variables](images/workspace-variables.png)

### Terraform Variables

```zsh
curl -X POST https://terrapod.example.com/api/v2/workspaces/ws-{id}/vars \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "vars",
      "attributes": {
        "key": "instance_type",
        "value": "t3.micro",
        "category": "terraform",
        "sensitive": false,
        "description": "EC2 instance type"
      }
    }
  }'
```

### Environment Variables

```zsh
curl -X POST https://terrapod.example.com/api/v2/workspaces/ws-{id}/vars \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "vars",
      "attributes": {
        "key": "AWS_REGION",
        "value": "eu-west-1",
        "category": "env",
        "sensitive": false
      }
    }
  }'
```

### Sensitive Variables

Set `"sensitive": true` for secrets. The value is protected by database encryption-at-rest and never returned in API responses:

```zsh
curl -X POST https://terrapod.example.com/api/v2/workspaces/ws-{id}/vars \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "vars",
      "attributes": {
        "key": "AWS_SECRET_ACCESS_KEY",
        "value": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "category": "env",
        "sensitive": true
      }
    }
  }'
```

Note: Sensitive variables are protected by database encryption-at-rest and never returned in API responses.

For managing variables across multiple workspaces, see variable sets (admin-only) in the web UI under **Admin > Variable Sets**.

![Variable Sets](images/admin-variable-sets.png)

---

## Setting Up the Private Registry

![Module Registry](images/registry-modules.png)

### Publishing a Module

1. Create the module:

```zsh
curl -X POST https://terrapod.example.com/api/terrapod/v1/registry-modules \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "registry-modules",
      "attributes": {
        "name": "vpc",
        "provider": "aws"
      }
    }
  }'
```

2. Create a version:

```zsh
curl -X POST https://terrapod.example.com/api/terrapod/v1/registry-modules/private/default/vpc/aws/versions \
  -H "Authorization: Bearer $TERRAPOD_TOKEN" \
  -H "Content-Type: application/vnd.api+json" \
  -d '{
    "data": {
      "type": "registry-module-versions",
      "attributes": {
        "version": "1.0.0"
      }
    }
  }'
```

3. Upload the module tarball using the presigned URL from the response.

4. Use the module in Terraform:

```hcl
module "vpc" {
  source  = "terrapod.example.com/default/vpc/aws"
  version = "1.0.0"
}
```

For full registry documentation, see [registry.md](registry.md).

---

## Connecting to VCS

To automatically trigger runs when you push to a Git repository:

1. Create a VCS connection (GitHub or GitLab)
2. Link a workspace to a repository

See [vcs-integration.md](vcs-integration.md) for detailed setup instructions.

---

## Uninstall

```zsh
helm uninstall terrapod --namespace terrapod
```

PersistentVolumeClaims (filesystem storage, ephemeral runner scratch) are not
removed by `helm uninstall` — delete them explicitly if you want to reclaim the
storage, and drop the namespace with `kubectl delete namespace terrapod`.

> Building and testing Terrapod from source — Tilt, live-reload, `make test` —
> is covered in [Local Development](local-development.md). You don't need any of
> that to run Terrapod.

---

## Next Steps

- [Authentication](authentication.md) -- configure OIDC/SAML identity providers
- [RBAC](rbac.md) -- set up roles and permissions
- [Drift Detection](drift-detection.md) -- enable scheduled infrastructure drift checks
- [Notifications](notifications.md) -- set up Slack, webhook, or email alerts on run events
- [Run Triggers](run-triggers.md) -- create cross-workspace dependency chains
- [Run Tasks](run-tasks.md) -- add pre/post-plan validation webhooks
- [Deployment](deployment.md) -- deploy to production
- [API Reference](api-reference.md) -- full API documentation
