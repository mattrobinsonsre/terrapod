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
     https://<terrapod>/api/v2/agent-pools/<pool-id>/listeners
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
   Listener connects to `GET /api/v2/listeners/{id}/events` via SSE. If the connection is being dropped:
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

- Listener appears in `GET /api/v2/agent-pools/<pool-id>/listeners`
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
     "https://<terrapod>/api/v2/admin/runs?filter[status]=queued" | jq '.meta.pagination.total-count'
   ```

2. **Check listener capacity:**
   ```bash
   curl -H "Authorization: Bearer $TOKEN" \
     https://<terrapod>/api/v2/agent-pools/<pool-id>/listeners | \
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
