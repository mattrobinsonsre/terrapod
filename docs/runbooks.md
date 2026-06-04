# Operational Runbooks

Failure-scenario runbooks for Terrapod operators. Each runbook covers: **Symptoms**, **Diagnosis**, **Resolution**, and **Verification**.

For metric queries, see [monitoring.md](monitoring.md). For state recovery, see [disaster-recovery.md](disaster-recovery.md). For deployment configuration, see [deployment.md](deployment.md).

---

## Stale Run (Errored After Timeout)

A run is marked "stale" by the reconciler when it has been in `planning` or `applying` status with no Job status update for longer than `staleTimeoutSeconds` (default: 1 hour).

### Symptoms

- Run shows status `errored` with message "Run stale — no Job status for >X:XX:XX"
- Workspace remains locked after run errors
- `terrapod_runs_terminal_total{status="errored"}` increasing

### Diagnosis

1. **Check if the runner Job exists:**
   ```bash
   kubectl get jobs -n <runner-ns> -l run-id=<run-id>
   ```

2. **Check if the Job's pod was scheduled:**
   ```bash
   kubectl get pods -n <runner-ns> -l job-name=<job-name>
   kubectl describe pod <pod-name> -n <runner-ns>
   ```

3. **Check the reconciler is running:**
   ```promql
   rate(terrapod_scheduler_task_executions_total{task="run_reconciler"}[5m])
   ```
   If zero, see [Scheduler Stall](#scheduler-stall).

4. **Check if any listener is online for the pool:**
   ```bash
   # Via API
   curl -H "Authorization: Bearer $TOKEN" \
     https://<terrapod>/api/terrapod/v1/agent-pools/<pool-id>/listeners
   ```
   If no listeners, see [Listener Offline](#listener-offline).

5. **Check listener logs for the `check_job_status` event:**
   ```logql
   {namespace="<ns>", pod=~"terrapod-listener.*"} |= "check_job_status" |= "<run-id>"
   ```

### Resolution

1. **Unlock the workspace** (if still locked after the run errored):
   ```bash
   curl -X POST -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/vnd.api+json" \
     https://<terrapod>/api/v2/workspaces/<ws-id>/actions/force-unlock
   ```

2. **Clean up the orphaned Job** (if it still exists):
   ```bash
   kubectl delete job <job-name> -n <runner-ns>
   ```

3. **Investigate root cause** — common causes:
   - **Node scheduling failure**: Pod stuck in `Pending` (insufficient resources, taints, node selector mismatch)
   - **Listener offline**: No listener to relay Job status back to the API
   - **Network partition**: Listener cannot reach the API to POST status updates
   - **Job completed but status not reported**: Redis may have evicted the status key before reconciler read it

4. **Re-queue the run** by creating a new run on the workspace.

### Verification

- New run completes successfully
- `terrapod_scheduler_task_executions_total{task="run_reconciler",status="success"}` is incrementing
- Workspace lock state is correct

---

## Runner OOM-Killed (#430)

A runner Job was killed by Kubernetes because its container exceeded its memory limit (`2 × workspace.resource_memory`). This usually shows up after a provider schema grows or a workspace acquires a lot more managed resources than it had when `resource_memory` was first set.

### Symptoms

- Run status `errored` with one of:
  - `Runner OOM-killed (peak memory N.NN Gi). Workspace resource_memory is XGi; limit is 2× that. Increase resource_memory + retry.`
  - `Runner killed (SIGKILL, exit 137) without an explicit K8s reason. Most likely OOM…`
- Run detail page shows the **Resource usage** panel with a red `OOM-killed` or `Killed (likely OOM)` badge
- AI plan summary (if enabled) is marked `skipped — runner exited abnormally (oom|killed)`
- No plan log uploaded, or only a partial log

### Diagnosis

1. **Confirm the failure mode** — open the Run detail page. The Resource usage panel shows peak memory next to the workspace's request + limit. If the memory bar is solid red, this is a true OOM.
2. **Check the runner_exit_status** — `oom` is definitive (we read it from `container.state.terminated.reason == "OOMKilled"`). `killed` is "exit 137 with no reason" — usually still OOM, but the pod was GCed before the listener could capture the terminated state; could also be a node-level eviction (e.g. spot instance preemption).
3. **Look at workload-shape changes** — has the workspace grown? `terraform state list | wc -l` against the latest applied state version, compared to a known-good run from before the OOM started happening.
4. **For `killed` runs, rule out preemption** — check node events around the run's `apply_started_at`. If the node disappeared (spot instance, autoscaler scale-down), it's not strictly OOM and the workspace is sized correctly; the run can be retried on a stable node.

### Resolution

**For true OOMs (`oom` status):**

1. Bump `resource_memory` on the workspace. UI: workspace settings. API: `PATCH /api/v2/workspaces/{id}` with `data.attributes.resource-memory`.
2. The limit auto-derives as `2 × request`, so doubling the request doubles the limit.
3. Re-queue the run (Retry button on the Run detail page).
4. After the new run succeeds, re-check the Resource usage panel — the bar should be in amber (80–95%) or green (<80%). If it's still red, bump again.

**Rule of thumb**: set `resource_memory` to ~50% above typical peak. Anything tracking ≥95% is one provider-schema change away from OOMing.

**For ambiguous `killed` runs:**

- If multiple consecutive runs `killed` at the same workspace, treat as OOM and bump `resource_memory` anyway.
- If only one isolated `killed` and the node is gone, it was preemption — retry as-is.

### Verification

- New run completes successfully
- Resource usage panel shows memory bar in green or amber, not red
- `runner_exit_status` is `clean` on the successful run (visible in the API response, not currently rendered in UI)

---

## State Diverged

The runner entrypoint marks a workspace as "state diverged" when an `apply` succeeds (infrastructure changed) but the state file upload to object storage fails. This is a critical situation — real infrastructure has changed but Terrapod's state doesn't reflect it.

### Symptoms

- Workspace shows `state_diverged = true` in the API
- Run shows status `errored` with a state upload failure message
- Runner Job logs show `STATE UPLOAD FAILED` followed by `POST .../state-diverged`

### Diagnosis

1. **Confirm the divergence:**
   ```bash
   curl -H "Authorization: Bearer $TOKEN" \
     https://<terrapod>/api/v2/workspaces/<ws-id> | jq '.data.attributes["state-diverged"]'
   ```

2. **Check the runner Job logs:**
   ```bash
   kubectl logs job/<job-name> -n <runner-ns>
   ```
   Or via Loki:
   ```logql
   {namespace="<runner-ns>", pod=~"tprun-<run-short>.*"} |= "state"
   ```

3. **Check object storage health:**
   ```promql
   rate(terrapod_storage_errors_total[5m])
   ```
   See [Storage Errors](#storage-errors) if elevated.

4. **Check if the state file was partially uploaded:**
   Look for the state key in object storage:
   ```bash
   # S3
   aws s3 ls s3://<bucket>/state/<workspace-id>/
   # Azure
   az storage blob list --container-name <container> --prefix state/<workspace-id>/
   ```

### Resolution

1. **If the state file exists in storage** (partial upload succeeded):
   - Download it, verify it's valid JSON, and re-upload via the API
   - Clear the diverged flag by uploading a new state version

2. **If no state file was uploaded:**
   - Run `terraform state pull` locally against the real infrastructure to regenerate state
   - Upload the recovered state via:
     ```bash
     curl -X POST -H "Authorization: Bearer $TOKEN" \
       -H "Content-Type: application/vnd.api+json" \
       -d '{"data":{"type":"state-versions","attributes":{"serial":<next-serial>,"md5":"<md5>","lineage":"<lineage>"}}}' \
       https://<terrapod>/api/v2/workspaces/<ws-id>/state-versions
     ```
   - Then upload the state content to the returned upload URL

3. **Clear the diverged flag** — uploading a new state version automatically clears `state_diverged`.

For detailed state recovery procedures, see [disaster-recovery.md](disaster-recovery.md).

### Verification

- `state_diverged` is `false` on the workspace
- A new plan-only run shows no unexpected changes (state matches reality)

---

## Scheduler Stall

All background tasks (reconciler, VCS poller, drift detection, audit retention) are coordinated via the distributed scheduler using Redis. If the scheduler stalls, no periodic tasks run across any replica.

### Symptoms

- Runs stuck in `planning`/`applying` indefinitely (reconciler not running)
- VCS changes not detected (poller not running)
- Drift checks stopped
- `terrapod_scheduler_task_executions_total` flat for all tasks

### Diagnosis

1. **Check scheduler metrics:**
   ```promql
   rate(terrapod_scheduler_task_executions_total[10m])
   ```
   Should show non-zero for `run_reconciler`, `vcs_poll`, `drift_check`, `audit_retention`.

2. **Check Redis scheduler keys:**
   ```bash
   kubectl exec -it <redis-pod> -n <ns> -- redis-cli
   # List all scheduler keys
   KEYS tp:sched:*
   # Check if a task is stuck in "running" state
   GET tp:sched:run_reconciler:running
   TTL tp:sched:run_reconciler:running
   # Check last execution time
   GET tp:sched:run_reconciler:last
   ```

3. **Check API pod logs for scheduler errors:**
   ```logql
   {namespace="<ns>", pod=~"terrapod-api.*"} |= "scheduler" |= "error"
   ```

4. **Check Redis connectivity:**
   ```bash
   kubectl exec -it <api-pod> -n <ns> -- python -c "
   import asyncio, redis.asyncio as redis
   async def check():
       r = redis.from_url('$REDIS_URL')
       print(await r.ping())
   asyncio.run(check())
   "
   ```

### Resolution

1. **If a task's "running" key is stuck** (TTL expired but key persists due to Redis issue):
   ```bash
   kubectl exec -it <redis-pod> -n <ns> -- redis-cli DEL tp:sched:run_reconciler:running
   ```

2. **If Redis is unreachable**, see [Redis Connection Loss](#redis-connection-loss).

3. **If API pods are healthy but scheduler isn't running**, restart the API pods:
   ```bash
   kubectl rollout restart deployment/terrapod-api -n <ns>
   ```

### Verification

- `terrapod_scheduler_task_executions_total` resumes incrementing
- Runs in `planning`/`applying` start transitioning again
- Check `/health` endpoint returns healthy for both `db` and `redis`

---

## Listener Offline

When no listener is available in a pool, runs cannot be claimed or executed. Queued runs accumulate.

### Symptoms

- Runs stuck in `queued` status indefinitely
- No listeners shown in pool detail page or API
- `terrapod_listener_heartbeats_total` flat for the pool
- Listener pods may be in CrashLoopBackOff or not running

### Diagnosis

1. **Check listener pod status:**
   ```bash
   kubectl get pods -n <ns> -l app.kubernetes.io/component=listener
   kubectl describe pod <listener-pod> -n <ns>
   ```

2. **Check listener logs:**
   ```bash
   kubectl logs <listener-pod> -n <ns> --tail=100
   ```
   Or via Loki:
   ```logql
   {namespace="<ns>", pod=~"terrapod-listener.*"} |= "error"
   ```

3. **Check Redis for listener data:**
   ```bash
   kubectl exec -it <redis-pod> -n <ns> -- redis-cli
   SMEMBERS tp:pool_listeners:<pool-id>
   # For each listener ID:
   HGETALL tp:listener:<listener-id>
   TTL tp:listener:<listener-id>
   ```

4. **Check certificate expiry:**
   Listener logs will show "certificate expired" if the certificate wasn't renewed. The renewal happens at 50% of validity.

5. **Check SSE connectivity:**
   Listener connects to `GET /api/terrapod/v1/listeners/{id}/events` via SSE. If the connection is being dropped:
   ```logql
   {namespace="<ns>", pod=~"terrapod-listener.*"} |= "SSE" |= "disconnect"
   ```

### Resolution

1. **If pods are crashing**, check logs for the root cause:
   - **Certificate errors**: Delete the listener's emptyDir (pod restart will re-join with new certs)
   - **Connection refused**: Verify the API service is reachable from the listener namespace
   - **Join token expired/revoked**: Create a new join token and update the listener's `TERRAPOD_JOIN_TOKEN`

2. **If pods are running but not appearing as listeners**, check that:
   - The join token is valid: `curl -X POST .../agent-pools/join`
   - Redis is reachable from the API (listener data is stored in Redis by the API)

3. **Force re-join** by restarting the listener deployment:
   ```bash
   kubectl rollout restart deployment/terrapod-listener -n <ns>
   ```

### Verification

- Listener appears in `GET /api/terrapod/v1/agent-pools/<pool-id>/listeners`
- `terrapod_listener_heartbeats_total{pool_id="..."}` incrementing
- `terrapod_listener_identity_ready` gauge is 1
- Queued runs start being claimed

---

## Run Queue Backlog

Runs are accumulating in `pending` or `queued` status faster than they can be processed.

### Symptoms

- Growing number of runs in `pending`/`queued` status
- Users report long wait times before plans start
- Workspace locks held for extended periods

### Diagnosis

1. **Count queued runs:**
   ```bash
   curl -H "Authorization: Bearer $TOKEN" \
     "https://<terrapod>/api/terrapod/v1/admin/runs?filter[status]=queued" | jq '.meta.pagination.total-count'
   ```

2. **Check listener capacity:**
   ```bash
   curl -H "Authorization: Bearer $TOKEN" \
     https://<terrapod>/api/terrapod/v1/agent-pools/<pool-id>/listeners | \
     jq '.data[] | {name: .attributes.name, active: .attributes["active-runs"], capacity: .attributes.capacity}'
   ```

3. **Check for scheduling issues on runner Jobs:**
   ```bash
   kubectl get pods -n <runner-ns> --field-selector=status.phase=Pending
   kubectl describe pod <pending-pod> -n <runner-ns>
   ```

4. **Check node resources:**
   ```bash
   kubectl top nodes
   kubectl describe nodes | grep -A 5 "Allocated resources"
   ```

### Resolution

1. **Scale up listeners** (if all at capacity):
   ```bash
   kubectl scale deployment/terrapod-listener -n <ns> --replicas=<N>
   ```

2. **Scale up cluster nodes** if runner Jobs can't be scheduled due to resource exhaustion.

3. **Reduce per-workspace resources** if Jobs are requesting more than needed:
   Update `resource_cpu` and `resource_memory` on workspaces via the API.

4. **Increase Job TTL cleanup** if completed Jobs are consuming scheduler slots:
   Set `runners.ttlSecondsAfterFinished` to a lower value.

### Verification

- Queued run count decreasing
- New runs are claimed within seconds of queueing
- `terrapod_listener_active_runs` shows listeners are processing

---

## DB Pool Exhaustion

The API server's SQLAlchemy connection pool is exhausted, causing requests to timeout or fail.

### Symptoms

- API returns 500 errors or times out
- `/health` endpoint returns unhealthy for `db`
- `terrapod_db_errors_total` increasing
- API logs show "QueuePool limit" or "TimeoutError" from SQLAlchemy

### Diagnosis

1. **Check health endpoint:**
   ```bash
   curl https://<terrapod>/api/v2/ping
   curl http://<api-pod-ip>:8000/health
   ```

2. **Check API logs for pool errors:**
   ```logql
   {namespace="<ns>", pod=~"terrapod-api.*"} |= "QueuePool" or |= "TimeoutError" or |= "pool"
   ```

3. **Check active DB connections:**
   ```sql
   SELECT count(*), state FROM pg_stat_activity
   WHERE datname = 'terrapod' GROUP BY state;
   ```

4. **Check pool configuration:**
   Current defaults: `pool_size=10`, `max_overflow=20`, `pool_timeout=30s`.
   Effective max connections per replica = `pool_size + max_overflow` = 30.

### Resolution

1. **Increase pool size** in Helm values:
   ```yaml
   api:
     config:
       database:
         pool_size: 20
         max_overflow: 40
   ```

2. **Check for connection leaks** — ensure no SSE endpoints use `Depends(get_db)` (this holds connections for the entire SSE stream lifetime).

3. **Scale API replicas** to distribute connection load:
   ```bash
   kubectl scale deployment/terrapod-api -n <ns> --replicas=<N>
   ```
   Note: total connections = `replicas * (pool_size + max_overflow)`. Ensure PostgreSQL `max_connections` can accommodate this.

4. **Enable pool_pre_ping** (default: `true`) to detect stale connections early.

### Verification

- `/health` returns healthy
- `terrapod_db_errors_total` rate drops to zero
- API response times return to normal

---

## Redis Connection Loss

Redis is the backbone for sessions, the scheduler, listener data, and live log streaming. A Redis outage has broad impact.

### Symptoms

- All user sessions invalidated (users redirected to login)
- Scheduler stalls (see [Scheduler Stall](#scheduler-stall))
- Listener registrations lost (see [Listener Offline](#listener-offline))
- Live log streaming stops
- `/health` endpoint returns unhealthy for `redis`
- `terrapod_redis_errors_total` increasing

### Diagnosis

1. **Check Redis connectivity from API pods:**
   ```bash
   kubectl exec -it <api-pod> -n <ns> -- python -c "
   import asyncio, redis.asyncio as redis
   async def check():
       r = redis.from_url('$REDIS_URL')
       print(await r.ping())
   asyncio.run(check())
   "
   ```

2. **Check Redis pod/service status:**
   ```bash
   kubectl get pods -n <ns> -l app=redis
   kubectl describe svc <redis-service> -n <ns>
   ```

3. **For ElastiCache/managed Redis**, check the cloud provider console for:
   - Failover events
   - Memory pressure (evictions)
   - Network connectivity issues

### Resolution

1. **If Redis pod is down**, restart it or let the StatefulSet controller recover it.

2. **If using ElastiCache Serverless** and seeing `ClusterCrossSlotError`, ensure all pipelines use `transaction=False` (see Redis Cluster Compatibility in CLAUDE.md).

3. **After Redis recovery:**
   - Sessions are lost — users must re-login
   - Listeners will re-register on their next heartbeat (or restart listener pods to force re-join)
   - Scheduler will resume automatically within one interval cycle
   - Live log data in Redis (`tp:log_stream:*`) is lost — check object storage for persisted logs

4. **If Redis is frequently failing**, consider:
   - Increasing Redis memory
   - Enabling persistence (AOF/RDB) for faster recovery
   - Using a managed Redis service with automatic failover

### Verification

- `/health` returns healthy for `redis`
- Users can log in and sessions persist
- Scheduler tasks resume (`terrapod_scheduler_task_executions_total` incrementing)
- Listeners re-appear in pool listings

---

## Storage Errors

Object storage (S3, Azure Blob, GCS, or filesystem) failures prevent state uploads, config downloads, log persistence, and cache operations.

### Symptoms

- State version uploads fail
- Runner Jobs fail during artifact download/upload
- `terrapod_storage_errors_total` increasing
- `terrapod_storage_operations_total{status="error"}` increasing

### Diagnosis

1. **Check storage metrics:**
   ```promql
   rate(terrapod_storage_errors_total[5m])
   rate(terrapod_storage_operations_total{status="error"}[5m])
   ```

2. **Check API logs for storage errors:**
   ```logql
   {namespace="<ns>", pod=~"terrapod-api.*"} |= "storage" |= "error"
   ```

3. **Provider-specific checks:**

   **S3:**
   ```bash
   # Check bucket access
   aws s3 ls s3://<bucket>/ --profile <profile>
   # Check IAM role (IRSA)
   kubectl describe sa <terrapod-sa> -n <ns>  # verify eks.amazonaws.com/role-arn annotation
   ```

   **Azure Blob:**
   ```bash
   az storage blob list --container-name <container> --account-name <account> --num-results 1
   # Check workload identity
   kubectl describe sa <terrapod-sa> -n <ns>  # verify azure.workload.identity/client-id annotation
   ```

   **GCS:**
   ```bash
   gsutil ls gs://<bucket>/
   # Check workload identity
   kubectl describe sa <terrapod-sa> -n <ns>  # verify iam.gke.io/gcp-service-account annotation
   ```

   **Filesystem:**
   ```bash
   kubectl exec -it <api-pod> -n <ns> -- ls -la /var/lib/terrapod/storage/
   kubectl exec -it <api-pod> -n <ns> -- df -h /var/lib/terrapod/storage/
   ```

4. **Check presigned URL expiry** — if using IRSA/WIF/WI, the credential lifetime must exceed the presigned URL expiry (default: 3600s).

### Resolution

1. **Permission errors**: Verify the ServiceAccount annotations match the IAM role/managed identity/service account. Restart pods after fixing SA annotations.

2. **Bucket/container not found**: Verify the storage configuration in Helm values matches the actual resource name.

3. **Filesystem full**: Expand the PVC or clean up old artifacts:
   ```bash
   kubectl exec -it <api-pod> -n <ns> -- du -sh /var/lib/terrapod/storage/*
   ```

4. **Network errors**: Check VPC endpoints (S3), private endpoints (Azure), or VPC Service Controls (GCS).

5. **Credential expiry (IRSA/WIF)**: Ensure the ServiceAccount token is being refreshed. Restart pods to force a new token projection.

### Verification

- `terrapod_storage_errors_total` rate drops to zero
- State uploads succeed (create a test state version)
- Runner Jobs can download configs and upload artifacts

---

## Autodiscovery rule didn't fire

**Symptom**: a PR added a directory matching what you thought your autodiscovery rule covers, but no workspace was auto-created.

### Diagnosis

Walk the rule evaluation logic from the outside in:

1. **Is the rule enabled?**
   ```sh
   curl -sk -H "Authorization: Bearer $TOKEN" \
     https://<terrapod>/api/terrapod/v1/autodiscovery-rules \
     | jq '.data[] | {name: .attributes.name, enabled: .attributes.enabled, repo: .attributes."repo-url"}'
   ```

2. **Did the poller see the PR?** Search the API logs for the rule's repo:
   ```sh
   kubectl logs deploy/terrapod-api --tail=2000 | grep -E "Autodiscovery|<your-repo>"
   ```
   You should see `Autodiscovery created workspace` (success) or `Autodiscovery name collision`/`Autodiscovery: cannot parse repo URL`/`Autodiscovery: failed to list PRs` (each is a clear cause).

3. **Does the rule's pattern actually match the file?** The pattern is matched against the *full path* (e.g. `accounts/alpha/network/main.tf`, not `main.tf`). Test the pattern in a Python REPL with the same engine:
   ```python
   from terrapod.services.workspace_autodiscovery_service import _match_glob
   _match_glob("accounts/alpha/network/main.tf", "accounts/*/**/*.tf")  # True/False
   ```

4. **Is the file actually a terraform file?** Only `*.tf`, `*.tfvars`, `*.tf.json`, `*.tfvars.json`, `*.hcl` trigger autodiscovery. README/CI changes don't.

5. **Is it caught by ignore_patterns?** Same `_match_glob` test against each ignore pattern.

6. **Did the workspace name collide with an existing unrelated workspace?** Search logs for `Autodiscovery name collision`. The collision skip is logged but never errored to the user. Tighten `name-template` to disambiguate (e.g. `name-template: "monorepo-{path}"`).

### Common gotchas

- **GitHub App permissions**: requires `Pull requests: read` (not just `Contents`). If the install was created before commit-status reporting was enabled, the App may lack pull-request scope. Re-grant in GitHub App settings.
- **Default-branch override**: a rule with `branch: ""` resolves to the repo's default branch. If you've renamed the default branch on GitHub but the rule was created before, the rule still tracks the default branch — no action needed. If you set `branch: "main"` literally and renamed to `master`, you need to update the rule.
- **Webhook payloads**: webhook-driven autodiscovery only fires for the repo named in the webhook payload. If you've configured the webhook on a different repo (or org-level webhook with selective filters), autodiscovery may only run on the next 60s poll cycle.

### Verification

After fixing the rule:
- Push a new commit to the test PR (small change to the `.tf` file is enough)
- Within 60s (or sooner via webhook), see the workspace appear in the list
- Workspace's `autodiscovery-rule-id` attribute references the rule

## Autodiscovered workspace stuck in `pending_deletion`

**Symptom**: an autodiscovered workspace shows a `Pending deletion` badge/banner and is not being acted on. This is **by design** — `pending_deletion` is the safe default (`on_directory_delete: flag`) and the terminal action is left to a human.

### Diagnosis

1. Read `lifecycle-reason` on the workspace (API attribute or the detail-page banner). Common reasons:
   - `directory '<dir>' removed on '<branch>'` — the tracked directory was deleted on the branch and the rule did **not** opt in to destroy.
   - `origin PR #<n> closed unmerged; workspace has state — needs an explicit operator action` — a speculative workspace that had already applied state, then its PR was abandoned.
   - `rename <old>-><new> but a workspace already owns <new>; needs an operator decision` — an ambiguous rename collision.
2. Decide intent: is the underlying infrastructure meant to be torn down, or was the directory removal a mistake?

### Resolution

- **Infra should be destroyed**: queue a destroy run on the workspace yourself (it still has its state), let it apply, then delete/archive the workspace. Do **not** flip the rule to `destroy` retroactively expecting it to pick up this workspace — the delete decision already happened; the rule policy is evaluated at branch-advance time.
- **Removal was a mistake**: restore the directory on the branch. The workspace stays as-is (lifecycle reconcile never auto-resurrects); set `lifecycle_state` back to `active` via a direct update once the directory is back, or recreate the workspace.
- Never bulk-destroy `pending_deletion` workspaces blindly — that state exists precisely so a human checks each one.

### Verification

- `lifecycle-state` is `active` (restored) or the workspace is gone (intentionally destroyed + deleted)
- No orphaned state versions remain for a destroyed workspace

## Reverting (or recovering from) a bad bulk-update

**Symptom**: a fleet `POST /api/terrapod/v1/workspaces/actions/bulk-update` applied an unintended change across many workspaces.

### Diagnosis

1. The bulk-update is **all-or-nothing in a single transaction** and **never triggers runs** — it is a pure settings write. So the blast radius is the settings delta only; no plans/applies were kicked off by the bulk-update itself. The change lands on each workspace's *next normal run*.
2. `dry_run` defaults to **on**. Confirm whether the offending call actually committed (`dry_run: false`) or was a preview.
3. Identify the exact field(s) changed and the selection filter used (audit log captures the bulk-update call).

### Resolution

- Re-issue the inverse bulk-update with the **same selection filter** and the previous values (settings are reversible — Terrapod keeps versioned state, and no run was triggered, so reverting the setting before the next run is a no-op in infra terms).
- If a normal run already picked up the unintended setting and applied it, treat that like any other unwanted apply: roll the workspace's state version back or apply a corrective change.

### Verification

- Re-query the affected workspaces (server-side workspace search) and confirm the field is restored
- No unexpected runs were created by the revert (bulk-update never triggers runs — if you see runs, they came from VCS/drift, not the bulk-update)

## Cross-workspace `terraform_remote_state` returns 403

**Symptom**: an agent-mode plan errors during init/refresh on a `data "terraform_remote_state"` data source pointing at another Terrapod workspace, with a 403 from `/api/v2/workspaces/{id}/current-state-version` or `/api/v2/state-versions/{id}/download`.

**Why**: cross-workspace state reads in agent mode are gated by the **producer's consumer allowlist** — the producer workspace explicitly lists which consumer workspaces may read its (secret-bearing) state. Default is empty. The runner token doesn't satisfy per-user RBAC for another workspace, so without an allowlist entry every cross-workspace read 403s. This is the security design — see [the composition guide](remote-state.md) and the [API reference](api-reference.md#cross-workspace-remote-state-consumers).

### Diagnosis

1. Confirm the consumer is in agent mode — local-mode CLI runs use the user's token and a 403 there means the user lacks `plan` on the producer (label-RBAC), not the allowlist.
2. Identify the producer workspace from the consumer's `terraform_remote_state` config (`workspaces.name`).
3. List the producer's current consumers: `GET /api/terrapod/v1/workspaces/ws-PRODUCER/remote-state-consumers?filter[remote-state-consumer][type]=outbound`. If the consumer isn't there, that's the cause.

### Resolution

Have the **producer's admin** authorize the consumer — via the Terrapod provider's `terrapod_remote_state_consumer` resource, the API directly, the bulk-update endpoint, or (if the consumer is autodiscovered) the rule's template. The grant is producer-controlled by design: a consumer team cannot self-grant. See the composition guide for the three equivalent paths.

### Verification

- `GET /api/terrapod/v1/workspaces/ws-PRODUCER/remote-state-consumers?filter[remote-state-consumer][type]=outbound` lists the consumer
- The consumer's next agent-mode plan proceeds past the `data "terraform_remote_state"` data source and resolves the outputs

### Producer deleted or archived

If the producer workspace was deleted, the grant rows cascade-deleted automatically and the consumer's next read returns 404 (no state). If the producer is `archived` via the [autodiscovery lifecycle](autodiscovery.md), the state is retained until purged — reads continue until then. Restoring an archived producer's grant requires recreating the workspace and re-authorizing the consumers.

---

## Policy enforcement blocking all runs

**Symptom**: after creating or editing an OPA policy set, runs across many (or all) workspaces stop advancing — they sit in `planning` and the run's **Policy Checks** panel shows a mandatory failure.

**Why**: a **mandatory**, broadly-scoped (often `global`) policy set has a policy that denies the plans. A mandatory failure holds the run in `planning` rather than erroring it, so the blast radius is "nothing applies" — recoverable, not destructive. See [policies.md](policies.md).

### Diagnosis

1. Open a blocked run's Policy Checks panel — it names the failing policy set and the specific `deny` messages.
2. Identify the set: `GET /api/terrapod/v1/policy-sets` — look for `enforcement-level: mandatory` and a broad scope (`global-scope: true` or wide allow-labels).
3. Decide whether the policy is correct-but-the-infra-is-wrong (fix the Terraform), or the policy itself is wrong/too broad.

### Resolution

Pick the least-disruptive option that fits:

- **Policy is wrong** — fix the Rego (`PATCH /api/terrapod/v1/policies/{id}`) or delete the offending policy. The next reconciler tick re-evaluates held runs automatically.
- **Set is too broadly scoped** — narrow its allow-labels, or set `enabled: false` on the set (`PATCH /api/terrapod/v1/policy-sets/{id}`) to stop it being evaluated. Disabling does not delete it.
- **Demote to advisory** — `PATCH` the set's `enforcement-level` to `advisory`; runs then proceed with a warning instead of a block. Note the enforcement level is *snapshotted per evaluation*, so already-recorded blocks are cleared by re-evaluation, not by the edit alone — held runs re-evaluate on the next tick.
- **Single urgent run** — a workspace admin can override one run from its Policy Checks panel ("Override & Continue").

### Verification

- `GET /api/terrapod/v1/runs/{id}/policy-evaluations` for a previously-blocked run shows the mandatory set now `passed` (or `overridden`).
- Held runs advance out of `planning` within one reconciler tick (~10s).

---

## Policy evaluation blocked: runner did not evaluate

**Symptom**: a run is held in `planning` and the Policy Checks panel shows one or more mandatory sets with outcome `errored` and a message starting with **"Runner did not evaluate this mandatory policy set."**

**Why**: OPA evaluation runs on the runner (since #343). For every applicable policy set, the runner is expected to POST a row to `/policy-results` before posting `plan-result`. The post-plan gate compares applicable sets to recorded rows and synthesises an `errored` row for any mandatory set that's missing one — fail-closed.

This usually means **the runner image is from before policy-as-code support / does not know about OPA evaluation**. Common during a Helm rolling upgrade when a node has an older runner image cached and `imagePullPolicy: IfNotPresent` keeps using it until the cache is GC'd. The runbook entry can also fire for a newer runner that posted `plan-result` successfully but failed to POST `/policy-results` for one or more applicable sets — uncommon because the runner entrypoint POSTs policy-results before plan-result and exits non-zero on POST failure (so usually the run is errored by the reconciler, not held at the gate).

### Diagnosis

1. Confirm the runner image. Check the Job pod's image: `kubectl get pods -n terrapod -l app.kubernetes.io/component=runner -o jsonpath='{.items[*].spec.containers[*].image}'`. If you see a SHA / tag from before the Terrapod release that introduced #343, the runner is stale.
2. Check the node's image cache: `kubectl describe pod <tprun-pod>` — the `Containers.runner.Image` and the events around `Pulled` / `Container image already present on machine` tell you whether the node pulled fresh or reused cache.

### Resolution

- **Roll the runner image forward.** Re-deploy with the matching Terrapod version (or restart the affected node so K8s pulls fresh). Future runs on this workspace will then evaluate cleanly.
- **Release this specific run** — a workspace admin can override the synthetic errored evaluation from the Policy Checks panel; the run resumes immediately.

### Verification

- A fresh run on the same workspace shows the policy set with a real outcome (`passed` / `failed`) rather than the synthetic `errored` message.
- The runner pod log includes `Fetching policy bundle...` / `Posting N policy evaluation result(s)` lines — confirms it's a post-#343 image.

---

## VCS commit status missing after run completion

**Symptom**: a workspace run completes but the corresponding commit status / PR check on GitHub or GitLab stays in `pending` / `running` forever. The Terrapod UI shows the run as `applied` / `errored` / `planned`. The PR check never advances.

**Why**: the dispatcher posting commit statuses (`vcs_status_dispatcher.py`) runs after a triggered task. Per-request retries (3 attempts on 429 / 5xx / transport errors) live in `github_service._github_request` and `gitlab_service._gitlab_request`. When those retries exhaust — typically a multi-minute VCS outage, an expired access token, or a token whose scopes were narrowed — the dispatcher logs an `error`-level line and moves on without re-enqueueing. The run itself is unaffected; only the upstream check display is.

The structured fields on the error log are: `provider`, `target_status`, `workspace_id`, `sha` (first 12 chars), `error`. Alert on:

```
service=terrapod-api logger=terrapod.services.vcs_status_dispatcher
  level=error
  msg="Failed to post VCS commit status (transport retries exhausted)"
```

### Diagnosis

1. **Check the access token validity.** For GitHub App connections, the App's installation token is fetched on-demand and cached for 50 minutes — if the App was uninstalled from the repo the next refresh 404s. For GitLab Project / Group access tokens, the token can expire or have its `read_repository` scope revoked.
2. **Confirm the VCS provider isn't itself degraded.** Check the provider's status page (`status.github.com` / `status.gitlab.com`). If there's an ongoing incident, the retries are doing their job; the error is expected and self-clears.
3. **Look for repeated entries for the same `(workspace_id, sha)`** — sustained failures across multiple runs point at auth, not transient. Single-run misses on different workspaces during the same window point at a VCS-side incident.

### Resolution

- **Token / scope issue**: rotate the token on the VCS connection (`/admin/vcs-connections`). For GitHub Apps, re-install the App with the necessary `Commit statuses: Write` permission.
- **VCS-side incident**: nothing to do; status will not auto-recover for already-failed posts (no re-enqueue). The next run on the same SHA, or any branch update, posts a fresh status.
- **Forcing a fresh post**: queue a no-op plan-only run on the same commit. This produces a new sequence of state transitions, each of which re-enqueues a commit-status update.

### Verification

- Run logs after the rotation show `level=info logger=terrapod.services.gitlab_service msg="GitLab commit status posted"` (or the equivalent for GitHub).
- The PR check on the next pushed commit lands within ~30 seconds of the run completing.

---

## AI plan-summary daily token budget exhausted

**Symptom**: AI plan summary panels on run detail pages start showing "Summary skipped for this run" (italic grey muted text) instead of the LLM description. New `plan_summaries` rows arrive with `status='skipped'` and `error_message='daily token budget exhausted'`. The run lifecycle itself is unaffected (plan / apply / lock state machine continues normally) — only the summary surface is muted.

**Why**: `api.config.ai_summary.daily_token_budget` caps the total *output* tokens spent across all summaries per UTC day. A Redis counter at `tp:ai_summary:budget:YYYYMMDD` (key TTL ~36h) accumulates `usage.completion_tokens` from every successful summariser call. When the next call would push past the cap, the handler short-circuits before invoking LiteLLM, writes a `status='skipped'` row, and emits the `plan_summary_ready` SSE event. The counter rolls over at the next UTC midnight automatically — no operator action is required for normal recovery.

### Diagnosis

1. **Confirm the budget actually exhausted** rather than a provider outage:
   ```bash
   kubectl exec -n <ns> <redis-pod> -- redis-cli GET "tp:ai_summary:budget:$(date -u +%Y%m%d)"
   # Compare against the configured cap:
   kubectl get configmap <release>-api-config -n <ns> -o jsonpath='{.data.config\.yaml}' \
     | grep -A1 ai_summary | grep daily_token_budget
   ```
   If the Redis value is at or above the cap, the budget is the cause. If well below, look at `plan_summaries.error_message` for the actual reason.

2. **Quantify the runaway** — query the last 24h of summaries:
   ```sql
   SELECT kind, status, count(*), sum(output_tokens) AS out_tokens
   FROM plan_summaries
   WHERE created_at > now() - interval '24 hours'
   GROUP BY kind, status ORDER BY out_tokens DESC;
   ```
   Look for a workspace or kind disproportionately consuming tokens (e.g. a flapping VCS workspace re-summarising the same plan many times).

### Resolution

- **Wait for UTC midnight reset.** If the spend is in line with normal usage and you just hit the cap because of a busy day, do nothing — the next plan after 00:00 UTC summarises again.
- **Bump the cap and `helm upgrade`** if the cap is too tight for legitimate steady-state use:
  ```yaml
  api:
    config:
      ai_summary:
        daily_token_budget: 10000000  # was 5000000
  ```
- **Set `daily_token_budget: 0`** for unlimited. Do this only if you have a separate cost guardrail upstream (provider-side spend limit, gateway quota, etc.).
- **Force-reset the counter** as an emergency measure (e.g. a runaway workspace already fixed):
  ```bash
  kubectl exec -n <ns> <redis-pod> -- redis-cli DEL "tp:ai_summary:budget:$(date -u +%Y%m%d)"
  ```
  The counter rebuilds from the next successful summary. Reset doesn't replay missed summaries — they stay `status='skipped'` for the historical runs.

### Verification

- Redis `tp:ai_summary:budget:<date>` is below the configured cap.
- New plans land with `plan_summaries.status='ready'` rather than `skipped`.
- The run-detail UI panel re-renders with markdown content within ~30 seconds of the plan reaching `planned`.

---

## AI plan-summary provider outage / credential failure

**Symptom**: AI summary panels show "Summariser failed" with an upstream error (HTTP 401 / 403 / 5xx, timeout, or model-not-found). The `plan_summaries` table accumulates rows with `status='errored'` and `error_message` carrying the LiteLLM exception. The run lifecycle is unaffected.

**Why**: the summariser's failure path is best-effort by design — every exception from `litellm.acompletion()` is caught, logged, and recorded as an `errored` row. The trigger handler never raises into the scheduler, so a sustained provider outage does NOT block plans, applies, VCS comments, or any other run state. Only the summary surface goes red.

### Diagnosis

1. **Inspect the most recent failure messages** to identify the upstream issue:
   ```sql
   SELECT kind, error_message, count(*)
   FROM plan_summaries
   WHERE status = 'errored' AND created_at > now() - interval '1 hour'
   GROUP BY kind, error_message ORDER BY count DESC;
   ```
   The error text comes straight from LiteLLM — Bedrock IAM, OpenAI 429, Anthropic 401, etc. each present distinctively.

2. **Confirm the provider isn't degraded** (Bedrock service health dashboard, OpenAI status, Anthropic status). Provider-side incidents auto-clear once upstream stabilises; no operator action is needed.

3. **For Bedrock IAM failures** specifically — verify the API pod's IRSA identity is still admitted by the cross-account trust policy on the `ai_summary.auth.aws_role_arn`:
   ```bash
   kubectl exec -n <ns> <api-pod> -- aws sts get-caller-identity
   kubectl exec -n <ns> <api-pod> -- aws sts assume-role \
     --role-arn "$(grep aws_role_arn /etc/terrapod/config.yaml | awk '{print $2}' | tr -d '"')" \
     --role-session-name diag
   ```

### Resolution

- **Provider outage**: no action; summaries auto-recover when upstream stabilises. The run-detail UI shows the latest errored row's message so reviewers see what's failing.
- **Credential / scope issue** (API key revoked, IRSA trust policy edited, cross-account role deleted): fix at the source. If using the bearer path, rotate the secret value:
  ```bash
  kubectl edit secret terrapod-ai-summary-credentials -n <ns>
  # update TERRAPOD_AI_SUMMARY__AUTH__API_KEY, save
  kubectl rollout restart deployment/<release>-api -n <ns>
  ```
- **Emergency disable** if the provider is down for an extended period and the red panels are visible to many reviewers, flip the global toggle off without restart-required code changes:
  ```yaml
  api:
    config:
      ai_summary:
        enabled: false
  ```
  followed by `helm upgrade ...`. The API pod re-reads its config on rollout and the trigger handler stops registering. All subsequent plans go through without ever attempting to summarise — no `plan_summaries` row is written at all (the handler short-circuits before the DB write). Re-enable when the upstream is healthy.

### Verification

- Errored rows stop accumulating: `SELECT count(*) FROM plan_summaries WHERE status='errored' AND created_at > now() - interval '15 minutes';` returns 0.
- The run-detail UI panel renders summaries with `status='ready'` (after re-enable + a new plan).
- If you emergency-disabled, no `plan_summaries` row appears for new runs and the UI panel doesn't render at all.
