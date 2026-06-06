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
- The canonical entrypoint is `python -m terrapod.runner.job_entrypoint` -- it drives signal forwarding, graceful shutdown, phase orchestration, and artifact uploads. Custom images should not override `ENTRYPOINT`; layer additional tooling on top via `RUN` and let the inherited entrypoint stand. (A transitional `/entrypoint.sh` bash script still exists in the image for unported phases — it's invoked by the Python entrypoint, not by the kubelet.)
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

## Injecting Environment Variables

There are two ways to get environment variables into runner Jobs:

### Workspace Variables (per-workspace)

Set variables on individual workspaces via the UI or API. Variables with `category=env` are injected as container environment variables. Sensitive variables are masked in API responses but the actual value is passed to the runner. Variable sets can share the same variables across multiple workspaces.

### Helm Values (global, all runners)

Use `runners.extraEnv` for individual env vars (literal values or from Secrets/ConfigMaps) and `runners.extraEnvFrom` to inject all keys from a Secret or ConfigMap:

```yaml
runners:
  # Individual env vars
  extraEnv:
    - name: AWS_DEFAULT_REGION
      value: eu-west-1
    - name: AWS_ACCESS_KEY_ID
      valueFrom:
        secretKeyRef:
          name: aws-credentials
          key: access-key-id
    - name: AWS_SECRET_ACCESS_KEY
      valueFrom:
        secretKeyRef:
          name: aws-credentials
          key: secret-access-key

  # Bulk inject all keys from a Secret or ConfigMap
  extraEnvFrom:
    - secretRef:
        name: aws-credentials
    - configMapRef:
        name: runner-config
```

Create the Secret in the runner namespace:

```bash
kubectl -n terrapod-runners create secret generic aws-credentials \
  --from-literal=access-key-id=AKIA... \
  --from-literal=secret-access-key=...
```

Helm-injected env vars apply to **all** runner Jobs globally. Use workspace variables when different workspaces need different credentials.

---

## Job Configuration

All runner Jobs inherit the following settings from `runners.*` in Helm values:

| Value | Default | Description |
|---|---|---|
| `runners.image.repository` | `ghcr.io/mattrobinsonsre/terrapod-runner` | Runner container image |
| `runners.image.tag` | `""` (appVersion) | Image tag |
| `runners.image.pullPolicy` | `IfNotPresent` | Image pull policy |
| `runners.imagePullSecrets` | `[]` | Pull secrets for private registries |
| `runners.extraEnv` | `[]` | Extra env vars for all runner Jobs |
| `runners.extraEnvFrom` | `[]` | Inject env vars from Secrets/ConfigMaps |
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

### Memory Pressure & OOM Visibility (#430)

Terraform/OpenTofu provider plugins can consume substantial memory — particularly the AWS provider when refreshing thousands of resources, or any provider that pulls a large module tarball into memory. If a runner Job hits its memory limit (`2 × resource_memory`), Kubernetes OOM-kills the container. Without explicit surfacing, the symptom is just a "Job failed" with no signal that memory was the cause — leaving an operator to guess + incrementally bump the limit. Terrapod surfaces it explicitly.

**Every run records its actual usage.** The runner entrypoint reads `/sys/fs/cgroup/memory.peak` (and `/sys/fs/cgroup/cpu.stat` for future use) at exit and POSTs them to `/api/terrapod/v1/runs/{run_id}/resource-profile`. The Run detail page renders a **Resource usage** panel showing peak memory **alongside the workspace's request and limit** — peak alone has no meaning, so it's always anchored. The memory bar turns amber at ≥80% of the limit and red at ≥95%, so high-water marks that are approaching the cliff are visible before they push a run over the edge.

CPU is captured but not yet surfaced. `peak_cpu_usec` from cgroup v2 is *cumulative* core-time, not an instantaneous peak — comparing it to the cores-allocated limit requires dividing by phase wall-clock, and even then the resulting *average* utilisation can hide bursts that briefly peg the limit. A proper CPU panel needs instantaneous sampling rather than cumulative-counter math; tracked as a follow-up. The data is still recorded so it's available the moment the sampling layer lands.

**OOM-killed runs are tagged explicitly.** SIGKILL is uncatchable, so the runner's own EXIT trap doesn't fire on OOM. The complementary signal comes from the **listener**: when a Job fails, the listener queries the pod's `container.state.terminated` field and reports the `reason` (`OOMKilled`) and `exit_code` (`137`) back via the job-status endpoint. The reconciler maps that to a typed `runner_exit_status`:

| `runner_exit_status` | Trigger | Error message points at |
|---|---|---|
| `oom` | K8s reason == `OOMKilled` | `resource_memory` (bump it + retry) |
| `killed` | Exit 137 with no explicit reason (pod GCed before we could read terminated state) | `resource_memory` as most likely cause, node eviction as the alternative |
| `error` | Non-zero exit, not 137 | Exit code |
| `clean` | Exit 0 (success path) | — |

The Run detail page surfaces an **OOM-killed / Killed (likely OOM)** badge when the status is `oom` or `killed`, and the error message itself names the actionable knob ("Workspace resource_memory is 1Gi … Increase resource_memory + retry") rather than the historical generic "Job failed".

**AI plan summary is skipped on abnormal exit.** When the runner is OOM-killed, the plan JSON upload usually never happens — summarising from a missing/empty plan would produce a confidently-wrong "no changes here" narrative. The summariser detects `runner_exit_status in {"oom", "killed"}` and marks the summary as `skipped` with an explicit reason, instead of generating a hallucinated one.

**Tuning workflow.** If a workspace OOMs, the operator's playbook is:

1. Check the Run detail page — confirm the OOM badge + peak memory shown.
2. The error message will name the current `resource_memory` value.
3. Bump it (UI workspace settings, or API `PATCH /api/v2/workspaces/{id}`) and retry. The limit will be `2 × resource_memory` automatically.
4. After the next successful run, re-check the Resource usage panel — the memory bar should be in the amber or green band, not red.

The peak data accumulates across runs (per-run snapshot on the row, plus visible on each Run detail page), so right-sizing the workspace is a matter of looking at a few representative runs' peaks and setting `resource_memory` to roughly half-again the typical peak. Anything tracking ≥95% (red) is one provider-schema change away from OOMing.

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

## Backend Neutralisation (Override File)

A workspace's committed configuration normally declares a `cloud {}` or `backend "remote" {}` block pointing at Terrapod. When that same configuration runs **inside a runner Job**, that block must not take effect — the runner executes locally and ships state back through the artifact endpoint. If the remote backend were honoured, the run would recursively call back into Terrapod.

The runner neutralises the backend with a **terraform-native override file** rather than editing the user's `.tf` files. Before `init`, the entrypoint writes `zzzz_terrapod_backend_override.tf` into the working directory:

```hcl
terraform {
  backend "local" {}
}
```

Terraform and OpenTofu merge any file matching `*_override.tf` over the main configuration with *replacement* semantics, so this single block displaces whatever the main config declared — `cloud {}`, `backend "remote" {}`, `backend "s3" {}`, or nothing at all. The user's committed files are never modified, which keeps "why does my plan differ locally" diagnosable.

**Our override always wins.** Local execution is a hard correctness requirement, so the override is written unconditionally — the runner never defers to a user-supplied backend declaration (deferring to a committed `cloud {}` or `backend "remote"` would hand the runner a remote backend and recurse). Two mechanisms enforce this:

1. **Merge order.** Override files are merged in lexical order with the *last* file winning. The `zzzz` filename prefix sorts after `override.tf` and the overwhelming majority of `*_override.tf` names, so Terrapod's `backend "local"` is the one that takes effect. If the workspace does ship its own override file declaring a backend/cloud block, the runner logs a `takes precedence` note so the override is visible — but still writes and wins with its own.
2. **Post-init backstop.** Merge order alone is not a hard guarantee — a user file sorting even later than `zzzz_…` would still win — so this is the real safety net. After `init`, the entrypoint reads the backend type that terraform/tofu actually configured (recorded in `.terraform/terraform.tfstate`). If it is anything other than `local`, the run **fails immediately** with a clear error rather than executing against a remote backend.

---

## OPA Policy Evaluation

Policy-as-code evaluation runs **on the runner**, between the plan phase and posting plan-result. The plan JSON is already on disk (just produced by `tofu show -json tfplan`), so the runner can evaluate OPA policies against it without a round-trip to storage and without any server-side concurrent-eval load. See [`docs/policies.md`](policies.md) for the authoring contract.

Sequence inside `runner-entrypoint.sh`, after `tofu plan` completes successfully:

1. `tofu show -json tfplan > /tmp/plan.json` — produces the JSON form used by both OPA and the `plan-json-output` artifact.
2. `tp_evaluate_policies` (the shell function in the entrypoint):
   - `GET /api/terrapod/v1/runs/{id}/policy-bundle` — fetches the policy sets in scope for this workspace, plus the run/workspace context. Bounded retries (3 attempts, 3s backoff); a persistent fetch failure is **fatal** to the run, never silently skipped.
   - For each applicable set, for each policy: `opa eval --format json --stdin-input --data <rego> --data <context> 'data.terrapod' < /tmp/plan.json`. Parses `deny` / `warn` from the OPA output. One eval per policy preserves per-policy attribution in the UI.
   - `POST /api/terrapod/v1/runs/{id}/policy-results` — uploads the aggregated results. Persisted via Postgres `ON CONFLICT DO NOTHING` on `(run_id, policy_set_id)` so retries are idempotent.
3. The runner posts `plan-result`. The API's post-plan gate is now a pure DB query — by this point the policy_evaluation rows already exist (or there were no applicable sets, which is the right answer too).

If `tofu show -json` failed but the plan succeeded, the runner records an `errored` outcome for every applicable set (fail-closed for mandatory sets). The OPA binary is pinned + SHA-verified in `docker/Dockerfile.runner` and the version is kept in sync with `Dockerfile.api` and `Dockerfile.test` — currently **OPA v1.16.2** with the **Rego v1 syntax** (`package … if {}` form). Bumping requires editing the `ARG OPA_VERSION=` in all three Dockerfiles and the matching `OPA_SHA256_*` constants.

This places eval workload exactly where the plan workload already lives — same pod, same resource budget, same K8s autoscaling. The API server stays out of the per-run hot path entirely; its only policy responsibilities are CRUD, write-time validation, the bundle endpoint, the results endpoint, and the gate query.

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

### Per-phase auth Secret

Each runner Job phase gets its own short-lived runner token, stored in a Kubernetes Secret with an `ownerReference` to the Job (so the Secret is garbage-collected when the Job's TTL expires). The Secret is named per phase to avoid collisions when plan and apply Jobs overlap during a fast transition:

```
tprun-<run-short-id>-plan-auth     # plan-phase Job consumes this
tprun-<run-short-id>-apply-auth    # apply-phase Job consumes this
```

The Job's pod spec references the token via `secretKeyRef` and exposes it as `TP_AUTH_TOKEN` — the raw token never appears in the Job spec, the listener logs, or `kubectl describe` output. The token is scoped to a single `run_id` and the matching phase, so a leaked apply token can't be replayed against an unrelated run or used to download a different workspace's state.

---

## Listener Identity

A listener is the long-lived Deployment that watches for runs and creates Job pods. It authenticates to the API with a short-lived X.509 certificate issued by the built-in CA. This section describes how that identity is bootstrapped, persisted, and renewed.

### Deployment-level identity

**Every pod in a listener Deployment shares one identity.** The cert/key/CA/listener-id live in a Kubernetes Secret named after the Deployment (`{release-fullname}-listener-credentials`) in the listener's own namespace. Multiple replicas read and write the same Secret using `resourceVersion` CAS, so pod replacement, rolling updates, and HPA scale-out never invalidate the identity. The Secret name is tied to the Deployment rather than `listener.name` so renaming the listener (changing its API-registered identity) doesn't orphan the Secret.

This is intentional: scaling the Deployment must not require new join tokens or new listener registrations on the API side. From the API's perspective a single `listener.name` corresponds to a single registered listener, regardless of replica count. (Each pod still heartbeats with a unique `{base_name}-{pod_name}` so stale pods can be tracked and aged out independently.)

### Bootstrap

On startup each pod runs the same flow:

1. Read the credentials Secret (name supplied via `TERRAPOD_CREDENTIALS_SECRET_NAME`, set by Helm to the Deployment fullname + `-credentials`). If it exists and has a valid cert/key/listener-id, adopt that identity and skip everything below.
2. If no Secret, call `POST /api/terrapod/v1/agent-pools/join` with `TERRAPOD_JOIN_TOKEN`. The API issues a short-lived cert and returns the full identity.
3. Try to `create` the credentials Secret with that identity. On `409 AlreadyExists` another pod won the race — re-read the Secret and adopt the winner's identity instead. The cert this pod was just issued is silently dropped.
4. If the join token is exhausted (`401`/`403`) before this pod gets a chance, the pod backs off (1, 2, 4, ... up to 30s, ~3 min total budget) and re-reads the Secret. As soon as a peer pod's bootstrap completes, the loser adopts that identity. This is why the default `max_uses: 2` is enough even for large replica counts — only the first two pods ever consume token uses, the rest discover the Secret.

The default join token policy (`api.config.agent_pools.default_join_token_*`) creates tokens with `max_uses: 2` and a 1h expiry. Set either field to `null` via the API for unlimited uses or no expiry. Setting `max_uses: 1` is also fine for single-replica deployments — the bootstrap-race retry only matters when you scale up before the first pod completes.

### Renewal

Each pod independently runs a renewal loop:

- The renewal threshold is `cert_validity_seconds / 2 + pod_splay_seconds`, where `pod_splay_seconds` is a deterministic SHA-256 hash of `POD_NAME` in the range `[0, 30)`. The splay desynchronises pods that started in lockstep so they don't all hit `/renew` simultaneously.
- When a pod reaches its threshold, it **re-reads the Secret first**. If the cert in the Secret still has more remaining lifetime than this pod's threshold (plus a 30s skew margin), another pod has already renewed it — adopt and reset the timer.
- Otherwise call `POST /api/terrapod/v1/listeners/{id}/renew` with up to 3 attempts (exponential backoff on 5xx / network errors; immediate failure on 401/403).
- On success, write the Secret with `resourceVersion` CAS. If another pod beat us to it (`409 Conflict`), re-read and adopt the peer's cert.
- On `401`/`403` from `/renew`, the cert is rejected (revoked, listener deleted, etc.) — clear the Secret and fall back to the join-token bootstrap flow.

The splay sits on the **renewal trigger**, not on startup. Adding sleeps to startup would fight Kubernetes scheduling and `minReadySeconds`; staggering renewal cycles is the right knob.

### Configuration

```yaml
# helm/terrapod/values.yaml
api:
  config:
    agent_pools:
      # Listener cert lifetime. Lower values surface bugs faster (default 1h).
      listener_cert_ttl_seconds: 3600
      # Defaults applied when admins create new join tokens via the API.
      # null = unlimited uses / no expiry.
      default_join_token_max_uses: 2
      default_join_token_ttl_seconds: 3600

listener:
  # Rolling update + minReadySeconds give the renewal cycles natural
  # stagger after a deploy: maxUnavailable=1 means one pod at a time,
  # minReadySeconds=30 spaces their POD_NAME-derived splays apart.
  strategy:
    maxSurge: 0
    maxUnavailable: 1
  minReadySeconds: 30
```

For Tilt local development, `values-local.yaml` overrides `listener_cert_ttl_seconds: 300` so a full bootstrap → renew → rotate cycle completes in a few minutes instead of an hour.

### Operational notes

- **Manual identity reset.** Delete the credentials Secret to force a fresh join on the next pod start: `kubectl -n terrapod delete secret {release-fullname}-listener-credentials` (find the exact name with `kubectl get secret -l app.kubernetes.io/component=listener`). This invalidates all running pods' certs on the next renewal. Rotate the join token first if the old one is no longer trusted.
- **Listener rename.** Because the Secret name follows the Deployment, changing `listener.name` rolls the API-registered identity but keeps the same Secret — the new identity simply overwrites it on next renewal. The old listener record in the API ages out of Redis when its heartbeats stop.
- **RBAC scope.** The listener ServiceAccount has `create` on Secrets in its own namespace and `get/list/watch/patch/update/delete` scoped by `resourceNames` to just the credentials Secret. The runner-namespace RBAC (Jobs, Pods, run-token Secrets) is separate.

---

## See Also

- [Architecture](architecture.md) -- runner listener, reconciler, ARC pattern
- [Cloud Credentials](cloud-credentials.md) -- workload identity setup
- [Deployment](deployment.md) -- full Helm configuration reference
- [Agent Pools](api-reference.md) -- pool and listener management
