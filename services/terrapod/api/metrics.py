"""Prometheus metrics instrumentation for the Terrapod API server.

Provides HTTP request counter/histogram, application-level metrics,
and a /metrics endpoint.  Only active when settings.metrics.enabled is True.

All metric objects are defined centrally here and imported at
instrumentation points (1-2 lines each).
"""

import time

from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# ---------------------------------------------------------------------------
# HTTP request metrics
# ---------------------------------------------------------------------------

REQUEST_COUNT = Counter(
    "terrapod_http_requests_total",
    "Total HTTP requests",
    ["method", "path_template", "status"],
)

REQUEST_DURATION = Histogram(
    "terrapod_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path_template", "status"],
)

# ---------------------------------------------------------------------------
# Run lifecycle metrics
# ---------------------------------------------------------------------------

RUNS_CREATED = Counter(
    "terrapod_runs_created_total",
    "Total runs created",
    ["source", "plan_only"],
)

RUNS_TRANSITIONED = Counter(
    "terrapod_runs_transitioned_total",
    "Total run state transitions",
    ["from_status", "to_status"],
)

RUNS_TERMINAL = Counter(
    "terrapod_runs_terminal_total",
    "Total runs reaching terminal state",
    ["status"],
)

RUN_PLAN_DURATION = Histogram(
    "terrapod_run_plan_duration_seconds",
    "Duration of run plan phase in seconds",
    ["status"],
)

RUN_APPLY_DURATION = Histogram(
    "terrapod_run_apply_duration_seconds",
    "Duration of run apply phase in seconds",
    ["status"],
)

# ---------------------------------------------------------------------------
# Scheduler metrics
# ---------------------------------------------------------------------------

SCHEDULER_TASK_EXECUTIONS = Counter(
    "terrapod_scheduler_task_executions_total",
    "Total periodic task executions",
    ["task", "status"],
)

SCHEDULER_TASK_DURATION = Histogram(
    "terrapod_scheduler_task_duration_seconds",
    "Duration of periodic task executions in seconds",
    ["task"],
)

SCHEDULER_TRIGGER_ENQUEUED = Counter(
    "terrapod_scheduler_trigger_enqueued_total",
    "Total triggers enqueued",
    ["type"],
)

SCHEDULER_TRIGGER_DEDUPLICATED = Counter(
    "terrapod_scheduler_trigger_deduplicated_total",
    "Total triggers deduplicated (skipped)",
    ["type"],
)

SCHEDULER_TRIGGER_PROCESSED = Counter(
    "terrapod_scheduler_trigger_processed_total",
    "Total triggers processed",
    ["type", "status"],
)

# ---------------------------------------------------------------------------
# VCS metrics
# ---------------------------------------------------------------------------

VCS_POLL_DURATION = Histogram(
    "terrapod_vcs_poll_duration_seconds",
    "Duration of VCS poll cycle in seconds",
    ["provider"],
)

VCS_COMMITS_DETECTED = Counter(
    "terrapod_vcs_commits_detected_total",
    "Total new commits detected by VCS poller",
    ["provider"],
)

VCS_PRS_DETECTED = Counter(
    "terrapod_vcs_prs_detected_total",
    "Total new PRs/MRs detected by VCS poller",
    ["provider"],
)

VCS_RUNS_CREATED = Counter(
    "terrapod_vcs_runs_created_total",
    "Total runs created by VCS poller",
    ["provider", "type"],
)

VCS_WEBHOOK_RECEIVED = Counter(
    "terrapod_vcs_webhook_received_total",
    "Total VCS webhook events received",
    ["provider"],
)

# ---------------------------------------------------------------------------
# Storage metrics
# ---------------------------------------------------------------------------

STORAGE_OPERATIONS = Counter(
    "terrapod_storage_operations_total",
    "Total storage operations",
    ["operation", "status"],
)

STORAGE_OPERATION_DURATION = Histogram(
    "terrapod_storage_operation_duration_seconds",
    "Duration of storage operations in seconds",
    ["operation"],
)

STORAGE_ERRORS = Counter(
    "terrapod_storage_errors_total",
    "Total storage operation errors",
    ["operation"],
)

# ---------------------------------------------------------------------------
# Auth metrics
# ---------------------------------------------------------------------------

AUTH_LOGIN = Counter(
    "terrapod_auth_login_total",
    "Total login attempts",
    ["provider", "outcome"],
)

AUTH_FAILURES = Counter(
    "terrapod_auth_failures_total",
    "Total authentication failures",
    ["method", "reason"],
)

# ---------------------------------------------------------------------------
# Cache metrics
# ---------------------------------------------------------------------------

BINARY_CACHE_REQUESTS = Counter(
    "terrapod_binary_cache_requests_total",
    "Total binary cache requests",
    ["tool", "result"],
)

PROVIDER_CACHE_REQUESTS = Counter(
    "terrapod_provider_cache_requests_total",
    "Total provider cache requests",
    ["result"],
)

# ---------------------------------------------------------------------------
# Infrastructure error metrics
# ---------------------------------------------------------------------------

DB_ERRORS = Counter(
    "terrapod_db_errors_total",
    "Total database errors",
    ["operation"],
)

REDIS_ERRORS = Counter(
    "terrapod_redis_errors_total",
    "Total Redis errors",
    ["operation"],
)

# ---------------------------------------------------------------------------
# State metrics
# ---------------------------------------------------------------------------

STATE_VERSIONS_CREATED = Counter(
    "terrapod_state_versions_created_total",
    "Total state versions created",
)

STATE_LOCK_CONFLICTS = Counter(
    "terrapod_state_lock_conflicts_total",
    "Total state lock conflicts (409)",
)


# ---------------------------------------------------------------------------
# Listener metrics (emitted from API, not from the listener itself)
# ---------------------------------------------------------------------------

LISTENER_HEARTBEATS = Counter(
    "terrapod_listener_heartbeats_total",
    "Total listener heartbeats received",
    ["pool_id"],
)

LISTENER_JOINS = Counter(
    "terrapod_listener_joins_total",
    "Total listener joins",
    ["pool_name"],
)


# ---------------------------------------------------------------------------
# Retention metrics
# ---------------------------------------------------------------------------

RETENTION_DELETED = Counter(
    "terrapod_retention_deleted_total",
    "Artifacts deleted by retention cleanup",
    ["category"],
)

RETENTION_ERRORS = Counter(
    "terrapod_retention_errors_total",
    "Errors during retention cleanup",
    ["category"],
)

RETENTION_DURATION = Histogram(
    "terrapod_retention_duration_seconds",
    "Duration of retention cleanup cycle",
)


def _get_path_template(request: Request) -> str:
    """Extract the FastAPI route pattern to avoid high-cardinality raw paths."""
    route = request.scope.get("route")
    if route and hasattr(route, "path"):
        return route.path
    return request.url.path


async def metrics_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Record request count and duration for every HTTP request."""
    if request.url.path == "/metrics":
        return await call_next(request)

    start = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - start

    path_template = _get_path_template(request)
    status = str(response.status_code)

    REQUEST_COUNT.labels(method=request.method, path_template=path_template, status=status).inc()
    REQUEST_DURATION.labels(
        method=request.method, path_template=path_template, status=status
    ).observe(duration)

    return response


async def metrics_endpoint(request: Request) -> Response:
    """Serve Prometheus metrics in exposition format."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
