"""Prometheus metrics instrumentation for the Terrapod API server.

Provides HTTP request counter/histogram, application-level metrics,
and a /metrics endpoint.  Only active when settings.metrics.enabled is True.

All metric objects are defined centrally here and imported at
instrumentation points (1-2 lines each).
"""

import time

from fastapi import Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

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

LISTENER_LAUNCH_FAILURES = Counter(
    "terrapod_listener_launch_failures_total",
    (
        "Pre-Job launch failures reported by listeners. Increments when a "
        "listener PATCHes a run to `errored` from `planning`/`applying` while "
        "the run still has no `job_name` — i.e. the listener claimed the run "
        "but couldn't get as far as launching the K8s Job (auth failure on "
        "/runner-token, create_job exception, auth Secret create failure)."
    ),
)

LISTENER_PRELAUNCH_TIMEOUTS = Counter(
    "terrapod_listener_prelaunch_timeouts_total",
    (
        "Reconciler timed out a run that was claimed but never had a Job "
        "launched. Counterpart to launch_failures: the listener didn't even "
        "manage to PATCH the failure (its cert was rejected, or it crashed). "
        "Backstop signal for silent listener failures."
    ),
)

POOL_QUEUED_RUNS = Gauge(
    "terrapod_pool_queued_runs",
    (
        "Runs currently in `queued` state per agent pool — the backlog waiting "
        "for a listener slot. Refreshed each reconciler cycle (2s). An operator "
        "watches this to see 'N runs waiting on pool X' and decide whether the "
        "pool needs more listener capacity (#750). A run whose workspace names "
        "several pools (#1085) is claimable by each of them and counts toward "
        "every one, so the sum across pools can exceed the number of queued "
        "runs."
    ),
    ["pool_id"],
)

HA_ROLE = Gauge(
    "terrapod_ha_role",
    (
        "1 when this node currently holds the given role, 0 otherwise (#960). "
        "Two alarms matter and they are opposites: no node reporting leader "
        "means the estate has silently stopped, and both nodes reporting "
        "leader means a split. Neither is inferable from a single node."
    ),
    ["role"],
)

HA_PROBE_AGE = Gauge(
    "terrapod_ha_probe_age_seconds",
    (
        "Seconds since this node last completed a leadership probe. Only "
        "meaningful under ha.role=auto; a stalled probe means the reported "
        "role is stale, which is a different failure from holding the wrong "
        "one (#960)."
    ),
)

POOL_LIVE = Gauge(
    "terrapod_pool_live",
    (
        "1 when an agent pool has at least one listener sending heartbeats, 0 "
        "otherwise. Refreshed each reconciler cycle (2s) from the same Redis "
        "heartbeat data the UI reads, so the dashboard and the alert can never "
        "disagree (#1085)."
    ),
    ["pool_id"],
)

WORKSPACES_WITHOUT_LIVE_POOL = Gauge(
    "terrapod_workspaces_without_live_pool",
    (
        "Agent-mode workspaces where NO pool in the workspace's pool set has a "
        "live listener — runs queued against them cannot execute. The SLO to "
        "alert on for execution-layer HA (#1085): multi-pool routing means "
        "losing one pool is survivable, so a non-zero value here means a "
        "workspace has lost all of its execution capacity."
    ),
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


# ── Replication (#960 phase 3, #1121) ────────────────────────────────────
#
# The follower's own view of whether it is converging, and the leader's view of
# how much margin its follower has. Both are answered from local state — a
# metric that needs the peer stops working exactly when the peer is the
# problem, which is when it is being read.

REPLICATION_SECONDS_SINCE_SYNC = Gauge(
    "terrapod_replication_seconds_since_last_sync",
    "Seconds since the last successful pull from the peer (follower side)",
)

REPLICATION_BACKFILLING_CLASSES = Gauge(
    "terrapod_replication_backfilling_classes",
    "Entity classes currently mid-backfill. Non-zero means NOT in sync, "
    "however recent the last cycle",
)

REPLICATION_EVENTS_RETAINED = Gauge(
    "terrapod_replication_events_retained",
    "Outbox events still inside the retention window",
)

REPLICATION_OLDEST_EVENT_AGE = Gauge(
    "terrapod_replication_oldest_event_age_seconds",
    "Age of the oldest retained outbox event. Approaching the retention window "
    "means the follower is close to falling off the end and having to backfill",
)


# ── In-cluster component readiness (#1122) ───────────────────────────────
#
# Ready AND desired, because `1` alone cannot distinguish a deliberately small
# deployment from one mid-incident. Absent entirely when the API lacks the
# namespace Role — a missing permission reports as unknown, not as zero.

COMPONENT_READY_REPLICAS = Gauge(
    "terrapod_component_ready_replicas",
    "Ready pods per Terrapod component in this namespace",
    ["component"],
)

COMPONENT_DESIRED_REPLICAS = Gauge(
    "terrapod_component_desired_replicas",
    "Desired replicas per Terrapod component, from its Deployment",
    ["component"],
)
