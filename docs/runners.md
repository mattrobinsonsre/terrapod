# Runners

Runners are ephemeral Kubernetes Jobs that execute `terraform` or `tofu` plan and apply operations. The default runner image is a minimal Alpine container with `curl`, `tar`, `jq`, `unzip`, and `git` -- no terraform/tofu binary baked in. The correct version is downloaded at runtime from the [binary cache](registry.md).

---

## Custom Runner Images

The default image covers most use cases, but you may need additional tools -- cloud CLIs (`aws`, `az`, `gcloud`), custom providers, policy tools, or language runtimes for scripts called by `local-exec` provisioners.

### Building a Custom Image

Base your image on the default runner and add what you need:

```dockerfile
FROM ghcr.io/mattrobinsonsre/terrapod-runner:latest

USER root

# Example: AWS CLI v2
RUN apk add --no-cache gcompat \
    && curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/awscliv2.zip /tmp/aws

# Example: Azure CLI
RUN apk add --no-cache py3-pip \
    && pip3 install --break-system-packages azure-cli

# Example: gcloud CLI
RUN curl -sSL https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz \
    | tar -xz -C /opt \
    && /opt/google-cloud-sdk/install.sh --quiet \
    && ln -s /opt/google-cloud-sdk/bin/gcloud /usr/local/bin/gcloud

USER 1000:1000
```

Important:
- Switch back to `USER 1000:1000` -- runner Jobs run as non-root
- The entrypoint (`/entrypoint.sh`) must remain unchanged -- it handles signal forwarding and graceful shutdown
- The working directory is `/workspace` and `/tmp` is writable (both are emptyDir volumes)

### Using a Custom Image

Set `runners.image` in your Helm values:

```yaml
runners:
  image:
    repository: registry.example.com/my-org/terrapod-runner-custom
    tag: "1.0.0"
    pullPolicy: IfNotPresent
```

### Private Registries

If your custom image is in a private registry, create a Kubernetes pull secret in the runner namespace and reference it:

```bash
kubectl -n terrapod-runners create secret docker-registry my-registry-secret \
  --docker-server=registry.example.com \
  --docker-username=user \
  --docker-password=token
```

```yaml
runners:
  imagePullSecrets:
    - my-registry-secret
```

Note: `runners.imagePullSecrets` is separate from `global.imagePullSecrets`. The global setting covers Helm-managed workloads (API, listener, web). Runner Jobs are created dynamically by the listener and need their own pull secrets configured here.

---

## Job Configuration

All runner Jobs inherit the following settings from `runners.*` in Helm values:

| Value | Default | Description |
|---|---|---|
| `runners.image.repository` | `ghcr.io/mattrobinsonsre/terrapod-runner` | Runner container image |
| `runners.image.tag` | `""` (appVersion) | Image tag |
| `runners.image.pullPolicy` | `IfNotPresent` | Image pull policy |
| `runners.imagePullSecrets` | `[]` | Pull secrets for private registries |
| `runners.nodeSelector` | `{}` | Node selector for Job pods |
| `runners.tolerations` | `[]` | Tolerations for Job pods |
| `runners.affinity` | `{}` | Affinity rules for Job pods |
| `runners.podAnnotations` | `{}` | Annotations added to Job pods |
| `runners.priorityClassName` | `""` | Priority class for Job pods |
| `runners.topologySpreadConstraints` | `[]` | Topology spread constraints |
| `runners.podSecurityContext` | `{}` | Pod-level security context override |
| `runners.ttlSecondsAfterFinished` | `600` | Clean up completed Jobs after this many seconds |
| `runners.terminationGracePeriodSeconds` | `120` | Time budget for graceful shutdown + artifact uploads |
| `runners.tokenTTLSeconds` | `3600` | Runner token TTL (1 hour) |
| `runners.maxTokenTTLSeconds` | `7200` | Maximum runner token TTL (2 hours) |
| `runners.staleTimeoutSeconds` | `3600` | Mark run as errored if no Job status after this long |

### Per-Workspace Resources

Each workspace has `resource_cpu` and `resource_memory` settings (default: 1 CPU / 2Gi memory) that control the resource **requests** for its runner Jobs. Limits are computed as 2x the requests automatically. These are set via the workspace API or UI.

---

## Cloud Workload Identity

Runner Jobs can assume cloud provider identities via Kubernetes ServiceAccount annotations. See [Cloud Credentials](cloud-credentials.md) for setup instructions.

```yaml
runners:
  serviceAccount:
    create: true
    annotations:
      # AWS IRSA
      eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/terrapod-runner
      # OR GCP WIF
      # iam.gke.io/gcp-service-account: runner@project.iam.gserviceaccount.com
      # OR Azure WI
      # azure.workload.identity/client-id: <client-id>
  # Required for Azure WI (adds pod label)
  azureWorkloadIdentity: false
```

---

## Graceful Termination

Runner Jobs implement time-budgeted graceful shutdown for spot instance preemption:

1. Kubernetes sends SIGTERM to the entrypoint
2. Entrypoint forwards **SIGINT** to terraform/tofu (HashiCorp's recommended signal for containers)
3. Watchdog monitors the child process -- if it doesn't exit within the grace period minus 25 seconds (upload budget), SIGKILL is sent
4. After terraform exits, artifact uploads (logs, plan, state) run with time-bounded `curl` calls
5. **State upload is fatal** -- if state upload fails after a successful apply, the workspace is flagged as state-diverged

The time budget adapts to the configured `terminationGracePeriodSeconds`. The default 120 seconds gives terraform 95 seconds to shut down gracefully and 25 seconds for artifact uploads.

---

## Environment Variables

The entrypoint reads the following environment variables (set automatically by the listener):

| Variable | Description |
|---|---|
| `TP_RUN_ID` | Run UUID |
| `TP_PHASE` | `plan` or `apply` |
| `TP_API_URL` | API base URL for artifact upload/download |
| `TP_AUTH_TOKEN` | Short-lived runner token (from K8s Secret) |
| `TP_VERSION` | Terraform/tofu version to download |
| `TP_BACKEND` | `terraform` or `tofu` |
| `TP_PLAN_ONLY` | `true` for plan-only runs |
| `TP_WORKING_DIR` | Subdirectory within the repo to run in |
| `TP_TARGET_ADDRS` | JSON array of `-target` addresses |
| `TP_REPLACE_ADDRS` | JSON array of `-replace` addresses |
| `TP_REFRESH_ONLY` | `true` for refresh-only mode |
| `TP_REFRESH` | `false` to disable refresh |
| `TP_ALLOW_EMPTY_APPLY` | `true` to allow empty applies |
| `TP_TERMINATION_GRACE` | Termination grace period in seconds |

Workspace variables (env and terraform) are also injected as environment variables on the Job pod.

---

## See Also

- [Architecture](architecture.md) -- runner listener, reconciler, ARC pattern
- [Cloud Credentials](cloud-credentials.md) -- workload identity setup
- [Deployment](deployment.md) -- full Helm configuration reference
- [Agent Pools](api-reference.md) -- pool and listener management
