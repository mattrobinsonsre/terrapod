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

The listener Deployment has a separate PodMonitor (`podmonitor-listener.yaml`), gated by `metrics.enabled`, `metrics.podMonitor.enabled`, and `listener.enabled` (all three must be true).

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

## Grafana dashboards

Terrapod ships an opinionated **Grafana dashboard** (`Terrapod — Overview`) covering API throughput/latency, run lifecycle, scheduler/reconciler health, listeners, VCS polling, caches, and infrastructure dependencies. The source JSON lives in the chart at [`helm/terrapod/dashboards/`](../helm/terrapod/dashboards/).

Two ways to get it into Grafana:

**Auto-import via the Grafana sidecar** (kube-prometheus-stack, bitnami Grafana). Enable the bundled ConfigMap — it's labeled `grafana_dashboard: "1"` so the sidecar picks it up automatically:

```yaml
api:
  config:
    metrics:
      enabled: true
      grafanaDashboards:
        enabled: true
        labels:
          grafana_dashboard: "1"   # match your sidecar's watch label
```

**Manual import.** In Grafana → *Dashboards → New → Import*, upload `helm/terrapod/dashboards/terrapod-overview.json` and pick your Prometheus data source.

The dashboard uses a templated `datasource` variable, so it works with any Prometheus data source — no hard-coded UID.

---

## Alerting (shipped PrometheusRule)

Terrapod ships a curated **`PrometheusRule`** (Prometheus Operator) so you don't have to author alerts from scratch. It's **off by default**; enable it and label it so your Prometheus's `ruleSelector` picks it up:

```yaml
api:
  config:
    metrics:
      enabled: true
      prometheusRule:
        enabled: true
        labels:
          release: kube-prometheus-stack   # match your ruleSelector
        # Optional: pin runbook links to a tag or an internal mirror.
        runbookBaseUrl: "https://github.com/mattrobinsonsre/terrapod/blob/main/docs/runbooks.md"
        # Optional: append your own spec.groups entries.
        extraGroups: []
```

Every alert carries a `runbook_url` annotation linking to the matching [operational runbook](runbooks.md). The bundled rules:

| Alert | Severity | Fires when | Runbook |
|---|---|---|---|
| `TerrapodAPIDown` | critical | No `terrapod-api` scrape target healthy for 2m | [API Down](runbooks.md#api-down--not-ready) |
| `TerrapodHighErrorRate` | warning | 5xx ratio > 5% over 5m | [High API Error Rate](runbooks.md#high-api-error-rate) |
| `TerrapodHighRequestLatency` | warning | p99 latency > 5s for 10m | [High API Latency](runbooks.md#high-api-latency) |
| `TerrapodRunsStuck` | warning | Runs enter planning/applying but none reach terminal in 30m | [Stale Run](runbooks.md#stale-run-errored-after-timeout) |
| `TerrapodListenerLaunchFailures` | warning | Listener claimed a run but couldn't launch its Job | [Listener Offline](runbooks.md#listener-offline) |
| `TerrapodListenerPrelaunchTimeouts` | warning | Reconciler timed out a claimed-but-never-launched run | [Listener Offline](runbooks.md#listener-offline) |
| `TerrapodReconcilerStalled` | critical | Run reconciler hasn't executed in 5m | [Scheduler Stall](runbooks.md#scheduler-stall) |
| `TerrapodSchedulerTaskFailing` | warning | A periodic task errors repeatedly over 15m | [Scheduler Stall](runbooks.md#scheduler-stall) |
| `TerrapodDatabaseErrors` | critical | DB error rate > 0.1/s for 5m | [DB Pool Exhaustion](runbooks.md#db-pool-exhaustion) |
| `TerrapodRedisErrors` | critical | Redis error rate > 0.1/s for 5m | [Redis Connection Loss](runbooks.md#redis-connection-loss) |
| `TerrapodStorageErrors` | warning | Object-storage errors for 5m | [Storage Errors](runbooks.md#storage-errors) |
| `TerrapodHighBinaryCacheMissRate` | info | Binary cache miss rate > 50% over 1h | [High Cache Miss Rate](runbooks.md#high-cache-miss-rate) |
| `TerrapodRetentionErrors` | warning | > 10 retention deletion errors in a day | [Storage Errors](runbooks.md#storage-errors) |

Thresholds are sensible defaults; to tune them, disable the bundled rule and copy the [template](../helm/terrapod/templates/prometheusrule-api.yaml), or add overriding rules via `extraGroups`.

> **`up{job=...}`** — the `TerrapodAPIDown` rule matches `job=~".*terrapod-api.*"`. The `job` label is derived by the Prometheus Operator from the scraped Service; if your relabeling produces a different value, adjust the matcher.
