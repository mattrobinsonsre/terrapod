# Monitoring

Terrapod exposes Prometheus metrics from two components: the **API server** (FastAPI) and the **web frontend** (Next.js). Metrics cover HTTP requests, run lifecycle, scheduler health, VCS polling, storage operations, authentication, caching, and infrastructure errors.

---

## Enabling Metrics

Metrics are gated by a single Helm value:

```yaml
api:
  config:
    metrics:
      enabled: true
```

When enabled:

- The **API** serves `/metrics` on its main port (8000) and registers HTTP request middleware for automatic request counting and duration tracking.
- The **web frontend** starts a separate metrics server on port **9091** via a Next.js `instrumentation.ts` hook. This port is added to the web Deployment and Service only when metrics are enabled.

---

## Security

The `/metrics` endpoints are **not exposed via the public ingress**:

- **API**: The BFF middleware only proxies `/api/*`, `/.well-known/*`, `/oauth/*`, `/v1/*`. The API's `/metrics` is never reachable from the internet.
- **Web**: Metrics are served on a **separate port** (9091). The ingress only routes to the web's main port (3000), so metrics are never reachable from the public URL.

Metrics are only reachable via **internal Service scraping** (Prometheus ServiceMonitor, `kubectl port-forward`, or direct pod access).

---

## Scraping

### ServiceMonitor (Prometheus Operator)

When both `metrics.enabled` and `metrics.serviceMonitor.enabled` are true, Terrapod creates ServiceMonitors for the API and web services:

```yaml
api:
  config:
    metrics:
      enabled: true
      serviceMonitor:
        enabled: true
        interval: "30s"    # scrape interval (default 30s)
        labels: {}         # extra labels for ServiceMonitor selection
```

The listener Deployment has a separate PodMonitor (`podmonitor-listener.yaml`), also gated by `metrics.enabled`.

| Component | Port | Path | ServiceMonitor |
|---|---|---|---|
| API | 8000 | `/metrics` | `servicemonitor-api.yaml` |
| Web | 9091 | `/metrics` | `servicemonitor-web.yaml` |
| Listener | (pod) | `/metrics` | `podmonitor-listener.yaml` |

### Annotation-Based Discovery

When metrics are enabled, Terrapod adds standard `prometheus.io/*` annotations to API and web pods:

| Component | `prometheus.io/scrape` | `prometheus.io/port` | `prometheus.io/path` |
|---|---|---|---|
| API | `"true"` | `8000` | `/metrics` |
| Web | `"true"` | `9091` | `/metrics` |

These annotations are used by Prometheus configurations that rely on `kubernetes_sd_configs` with annotation-based relabeling (the older, non-Operator pattern). If you're using ServiceMonitors (Prometheus Operator), the annotations are harmless but redundant.

### Manual Scrape Config

If not using the Prometheus Operator:

```yaml
scrape_configs:
  - job_name: terrapod-api
    kubernetes_sd_configs:
      - role: service
        namespaces:
          names: [terrapod]
    relabel_configs:
      - source_labels: [__meta_kubernetes_service_name]
        regex: terrapod-api
        action: keep
    metrics_path: /metrics

  - job_name: terrapod-web
    kubernetes_sd_configs:
      - role: service
        namespaces:
          names: [terrapod]
    relabel_configs:
      - source_labels: [__meta_kubernetes_service_name]
        regex: terrapod-web
        action: keep
      - source_labels: [__meta_kubernetes_service_port_name]
        regex: metrics
        action: keep
    metrics_path: /metrics
```

---

## Metrics Reference

### HTTP (API)

| Metric | Type | Labels | Description |
|---|---|---|---|
| `terrapod_http_requests_total` | Counter | method, path_template, status | Total HTTP requests |
| `terrapod_http_request_duration_seconds` | Histogram | method, path_template, status | Request duration |

### Runs

| Metric | Type | Labels | Description |
|---|---|---|---|
| `terrapod_runs_created_total` | Counter | source, plan_only | Runs created |
| `terrapod_runs_transitioned_total` | Counter | from_status, to_status | Run state transitions |
| `terrapod_runs_terminal_total` | Counter | status | Runs reaching terminal state |
| `terrapod_run_plan_duration_seconds` | Histogram | status | Plan phase duration |
| `terrapod_run_apply_duration_seconds` | Histogram | status | Apply phase duration |

### Scheduler

| Metric | Type | Labels | Description |
|---|---|---|---|
| `terrapod_scheduler_task_executions_total` | Counter | task, status | Periodic task executions |
| `terrapod_scheduler_task_duration_seconds` | Histogram | task | Periodic task duration |
| `terrapod_scheduler_trigger_enqueued_total` | Counter | type | Triggers enqueued |
| `terrapod_scheduler_trigger_deduplicated_total` | Counter | type | Triggers skipped (dedup) |
| `terrapod_scheduler_trigger_processed_total` | Counter | type, status | Triggers processed |

### VCS

| Metric | Type | Labels | Description |
|---|---|---|---|
| `terrapod_vcs_poll_duration_seconds` | Histogram | provider | Poll cycle duration |
| `terrapod_vcs_commits_detected_total` | Counter | provider | New commits detected |
| `terrapod_vcs_prs_detected_total` | Counter | provider | New PRs/MRs detected |
| `terrapod_vcs_runs_created_total` | Counter | provider, type | Runs created by VCS poller |
| `terrapod_vcs_webhook_received_total` | Counter | provider | Webhook events received |

### Storage

| Metric | Type | Labels | Description |
|---|---|---|---|
| `terrapod_storage_operations_total` | Counter | operation, status | Storage operations |
| `terrapod_storage_operation_duration_seconds` | Histogram | operation | Storage operation duration |
| `terrapod_storage_errors_total` | Counter | operation | Storage errors |

### Auth

| Metric | Type | Labels | Description |
|---|---|---|---|
| `terrapod_auth_login_total` | Counter | provider, outcome | Login attempts |
| `terrapod_auth_failures_total` | Counter | method, reason | Authentication failures |

### Cache

| Metric | Type | Labels | Description |
|---|---|---|---|
| `terrapod_binary_cache_requests_total` | Counter | tool, result | Binary cache requests (hit/miss) |
| `terrapod_provider_cache_requests_total` | Counter | result | Provider cache requests (hit/miss) |

### Infrastructure

| Metric | Type | Labels | Description |
|---|---|---|---|
| `terrapod_db_errors_total` | Counter | operation | Database errors |
| `terrapod_redis_errors_total` | Counter | operation | Redis errors |

### State

| Metric | Type | Labels | Description |
|---|---|---|---|
| `terrapod_state_versions_created_total` | Counter | -- | State versions created |
| `terrapod_state_lock_conflicts_total` | Counter | -- | Lock conflicts (409) |

### Retention

| Metric | Type | Labels | Description |
|---|---|---|---|
| `terrapod_retention_deleted_total` | Counter | category | Artifacts deleted by retention cleanup |
| `terrapod_retention_errors_total` | Counter | category | Errors during retention cleanup |
| `terrapod_retention_duration_seconds` | Histogram | -- | Duration of retention cleanup cycle |

Categories: `state_versions`, `run_artifacts`, `config_versions`, `provider_cache`, `binary_cache`, `module_overrides`.

### Listeners (API-side)

These metrics are emitted by the **API server**, since it receives all heartbeats and join requests:

| Metric | Type | Labels | Description |
|---|---|---|---|
| `terrapod_listener_heartbeats_total` | Counter | pool_id | Heartbeats received from listeners |
| `terrapod_listener_joins_total` | Counter | pool_name | Listener join events |

### Listeners (Self-Reported)

The listener process itself exposes metrics on its health port (`8081` at `GET /metrics`), scraped via the PodMonitor:

| Metric | Type | Description |
|---|---|---|
| `terrapod_listener_active_runs` | Gauge | Number of currently active runner Jobs |
| `terrapod_listener_identity_ready` | Gauge | 1 if identity established, 0 otherwise |
| `terrapod_listener_heartbeat_age_seconds` | Gauge | Seconds since last successful heartbeat (-1 if never) |

### Web (Next.js)

| Metric | Type | Labels | Description |
|---|---|---|---|
| `terrapod_web_page_requests_total` | Counter | path | Page/route handler requests |
| `terrapod_web_metrics_scrapes_total` | Counter | -- | Metrics endpoint scrapes |
| Node.js default metrics | various | -- | GC, event loop, memory (via prom-client) |

---

## Recommended Alerts

### High Error Rate

```yaml
- alert: TerrapodHighErrorRate
  expr: |
    sum(rate(terrapod_http_requests_total{status=~"5.."}[5m]))
    / sum(rate(terrapod_http_requests_total[5m])) > 0.05
  for: 5m
  annotations:
    summary: "Terrapod API error rate above 5%"
```

### Stuck Runs

```yaml
- alert: TerrapodStuckRuns
  expr: |
    increase(terrapod_runs_transitioned_total{to_status=~"planning|applying"}[30m]) > 0
    unless increase(terrapod_runs_terminal_total[30m]) > 0
  for: 30m
  annotations:
    summary: "Runs entering planning/applying but none reaching terminal state"
```

### Scheduler Stalls

```yaml
- alert: TerrapodSchedulerStall
  expr: |
    increase(terrapod_scheduler_task_executions_total{task="run_reconciler"}[5m]) == 0
  for: 5m
  annotations:
    summary: "Run reconciler has not executed in 5 minutes"
```

### Storage Errors

```yaml
- alert: TerrapodStorageErrors
  expr: rate(terrapod_storage_errors_total[5m]) > 0
  for: 5m
  annotations:
    summary: "Storage backend errors detected"
```

### Database/Redis Errors

```yaml
- alert: TerrapodInfraErrors
  expr: |
    rate(terrapod_db_errors_total[5m]) > 0.1
    or rate(terrapod_redis_errors_total[5m]) > 0.1
  for: 5m
  annotations:
    summary: "Database or Redis errors detected"
```

### Cache Miss Rate

```yaml
- alert: TerrapodHighCacheMissRate
  expr: |
    sum(rate(terrapod_binary_cache_requests_total{result="miss"}[1h]))
    / sum(rate(terrapod_binary_cache_requests_total[1h])) > 0.5
  for: 1h
  annotations:
    summary: "Binary cache miss rate above 50% over the last hour"
```

### Retention Errors

```yaml
- alert: TerrapodRetentionErrors
  expr: increase(terrapod_retention_errors_total[1d]) > 10
  for: 1h
  annotations:
    summary: "Artifact retention encountering persistent errors"
    description: "More than 10 retention deletion errors in the last day. Check storage backend health."
```
