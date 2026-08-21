"""
FastAPI application factory for Terrapod API server.

Uses lifespan handler for startup/shutdown with async resource management.
"""

import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from terrapod.auth.connectors import init_connectors
from terrapod.config import settings
from terrapod.db.session import close_db, get_db_session, init_db
from terrapod.logging_config import configure_logging, get_logger
from terrapod.redis.client import close_redis, init_redis
from terrapod.storage import close_storage, init_storage

from .errors import UPSTREAM_FAILURE_HEADER
from .health import router as health_router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application lifespan handler for startup and shutdown."""
    # Startup
    configure_logging(json_logs=settings.json_logs, log_level=settings.log_level)
    logger.info("Starting Terrapod API server", version="0.1.0")

    await init_db()
    logger.info("Database initialized")

    # App ↔ schema skew guard (#544): warm the code-head cache and loudly warn
    # if the DB schema is behind this app's expected migration head. Doesn't
    # raise — a schema-behind pod boots and reports NOT READY via /ready.
    from terrapod.db.schema_version import check_schema_at_startup

    await check_schema_at_startup()

    await init_redis()
    logger.info("Redis initialized")

    init_connectors()
    logger.info("Auth connectors initialized")

    await init_storage()
    logger.info("Storage initialized")

    # Replication outbox hooks (#960 phase 3, #1110). Installed before any DB
    # write so no change can slip past unrecorded — but ONLY when this node is
    # actually part of a pair (#1117).
    #
    # `ha.peer.url` is the signal because BOTH nodes set it: the leader needs it
    # for when the roles swap and it becomes the puller. A single-node install
    # leaves it empty and does no replication work at all.
    #
    # An earlier version recorded unconditionally, on the reasoning that a node
    # which gains a peer later should not have a hole in its outbox. That does
    # not hold since #1115: the new peer backfills each class from scratch, and
    # backfill reconciles — so the hole is irrelevant, and the cost was being
    # paid by the overwhelming majority of installs for nothing.
    if settings.ha.peer.url:
        from terrapod.services import replication

        replication.install_outbox_hooks()

    # Initialize app-layer encryption at rest (#553) BEFORE the CA — the CA
    # private key column is EncryptedText, so the service must be ready first.
    # Fail CLOSED when encryption is enabled (a wrong/missing key must crash);
    # tolerate errors only when disabled (e.g. table missing pre-migration).
    from terrapod.config import settings as _settings
    from terrapod.crypto.service import init_encryption

    try:
        async with get_db_session() as db:
            await init_encryption(db)
    except Exception as e:
        if _settings.encryption.enabled:
            raise
        logger.warning("Encryption init skipped (disabled; migration may be pending)", error=str(e))

    # Initialize Certificate Authority
    from terrapod.auth.ca import init_ca

    try:
        async with get_db_session() as db:
            await init_ca(db)
        logger.info("Certificate Authority initialized")
    except Exception as e:
        logger.warning("CA initialization skipped (migration may be pending)", error=str(e))

    # Register and start distributed scheduler (multi-replica safe)
    from terrapod.services.scheduler import (
        AI_LANE,
        register_periodic_task,
        register_trigger_handler,
        start_scheduler,
        stop_scheduler,
    )

    if settings.vcs.enabled:
        from terrapod.services.vcs_poller import handle_immediate_poll, poll_cycle

        register_periodic_task(
            "vcs_poll",
            interval_seconds=settings.vcs.poll_interval_seconds,
            handler=poll_cycle,
            description="Poll VCS providers for new commits and PRs",
        )
        register_trigger_handler(
            "vcs_immediate_poll",
            handler=handle_immediate_poll,
            description="Webhook-triggered immediate VCS poll",
        )

        # VCS commit status posting (commit statuses + PR comments)
        from terrapod.services.vcs_status_dispatcher import handle_vcs_commit_status

        register_trigger_handler(
            "vcs_commit_status",
            handler=handle_vcs_commit_status,
            description="Post commit status to VCS on run state change",
        )

        # Registry module VCS publishing (piggybacks on VCS being enabled)
        from terrapod.services.registry_vcs_poller import (
            handle_registry_vcs_immediate_poll,
            registry_vcs_poll_cycle,
        )

        # Module repos poll on their own, longer interval (#1149): they are far
        # more numerous than they are active. No webhook accelerator exists for
        # tag publishing, so this interval IS the auto-publish latency.
        register_periodic_task(
            "registry_vcs_poll",
            interval_seconds=settings.vcs.module_poll_interval_seconds,
            handler=registry_vcs_poll_cycle,
            description="Poll VCS providers for new module version tags",
        )
        # Tag publishing used to be the only VCS poller with no accelerator, so
        # its interval was the publish latency. The tag-push events were already
        # arriving; they just were not wired here (#1149).
        register_trigger_handler(
            "registry_vcs_immediate_poll",
            handler=handle_registry_vcs_immediate_poll,
            description="Webhook-triggered immediate module tag poll",
        )

        # Policy set VCS syncing
        from terrapod.services.policy_vcs_poller import (
            handle_policy_vcs_sync,
            policy_vcs_poll_cycle,
        )

        register_periodic_task(
            "policy_vcs_poll",
            interval_seconds=settings.vcs.poll_interval_seconds,
            handler=policy_vcs_poll_cycle,
            description="Sync VCS-connected policy sets from git repos",
        )
        register_trigger_handler(
            "policy_vcs_sync",
            handler=handle_policy_vcs_sync,
            description="Triggered immediate sync for a VCS policy set",
        )

        # Module impact analysis: speculative plans for module PRs
        from terrapod.services.module_impact_service import (
            handle_module_impact_immediate_poll,
            handle_module_test_completed,
            module_impact_poll_cycle,
        )

        # Also a module repo, so the module interval (#1149). Unlike tag
        # publishing this one HAS a webhook accelerator
        # (`module_impact_immediate_poll` below), so PR blast-radius feedback is
        # not gated on the interval where webhooks are configured.
        register_periodic_task(
            "module_impact_poll",
            interval_seconds=settings.vcs.module_poll_interval_seconds,
            handler=module_impact_poll_cycle,
            description="Poll VCS-connected modules for open PRs and create speculative runs",
        )

        register_trigger_handler(
            "module_impact_immediate_poll",
            handler=handle_module_impact_immediate_poll,
            description="Webhook-triggered immediate module impact poll",
        )

        register_trigger_handler(
            "module_test_completed",
            handler=handle_module_test_completed,
            description="Post VCS status when module-test run completes",
        )

    # VCS comment dispatcher (#282 apply-then-merge). Always registered —
    # it's a no-op for comments on PRs without an apply-then-merge workspace.
    from terrapod.services.vcs_command_dispatcher import handle_vcs_comment_dispatch
    from terrapod.services.vcs_status_comment import handle_vcs_status_comment_update

    register_trigger_handler(
        "vcs_comment_dispatch",
        handler=handle_vcs_comment_dispatch,
        description="Parse and dispatch `terrapod ...` PR/MR comments",
    )
    register_trigger_handler(
        "vcs_status_comment_update",
        handler=handle_vcs_status_comment_update,
        description="Edit-in-place PR status comment for apply-then-merge PRs",
    )

    from terrapod.services.vcs_auto_merge import handle_vcs_apply_completed

    register_trigger_handler(
        "vcs_apply_completed",
        handler=handle_vcs_apply_completed,
        description="Cross-workspace merge gate evaluation + auto-merge",
    )

    # Notification delivery handler (always registered)
    from terrapod.services.notification_dispatcher import handle_notification_delivery

    register_trigger_handler(
        "notification_deliver",
        handler=handle_notification_delivery,
        description="Deliver workspace notification on run state change",
    )

    # Onboarding D1 schema discovery (#824) — runs the credential-less
    # tofu init + terrapod-query schema off the request thread. Always
    # registered; a no-op unless a session enqueues it.
    from terrapod.services.onboarding_service import handle_schema_discover_trigger

    register_trigger_handler(
        "onboarding_schema_discover",
        handler=handle_schema_discover_trigger,
        description="Run credential-less onboarding D1 schema discovery for a session",
    )

    # AI onboarding config polish (#824 Phase A) — rename discovered resources
    # from their tags, group, and comment. Always registered; the handler
    # self-gates on settings.ai_onboarding.enabled and only fires once a session
    # reaches config_ready with generated config.
    from terrapod.services.onboarding_ai_service import handle_onboarding_polish

    register_trigger_handler(
        "onboarding_polish",
        handler=handle_onboarding_polish,
        description="AI-polish an onboarding session's generated config (naming/grouping/comments)",
    )

    # AI architecture critic (#1036 Part 2) — state-based whole-system critique.
    # Always registered; the handler self-gates on settings.ai_architecture.enabled.
    from terrapod.services.architecture_critic_service import handle_architecture_critique

    register_trigger_handler(
        "architecture_critique",
        handler=handle_architecture_critique,
        description="Generate the AI architecture critique for a workspace's current state",
    )

    # Slack app run notifications (#556) — approval / applied / errored / drift.
    # Registered only when the Slack app is enabled; the handler also no-ops
    # unless the target workspace opted in with its own channel.
    if settings.slack.enabled:
        from terrapod.services.slack_notify_service import (
            handle_slack_run_notify,
            slack_approval_backfill_cycle,
        )

        register_trigger_handler(
            "slack_run_notify",
            handler=handle_slack_run_notify,
            description="Post/update the Slack run message for a run event",
        )
        # Safety net (#687): post any needs-approval message that was deferred
        # to the AI summariser but never fired (e.g. the runner died before the
        # plan-JSON upload). No-ops unless AI is on (nothing is deferred then).
        register_periodic_task(
            "slack_approval_backfill",
            interval_seconds=60,
            handler=slack_approval_backfill_cycle,
            description="Backfill deferred Slack approval posts the summariser missed",
        )

    # Run task webhook delivery handler
    from terrapod.services.run_task_dispatcher import handle_run_task_call

    register_trigger_handler(
        "run_task_call",
        handler=handle_run_task_call,
        description="Deliver run task webhook to external service",
    )

    # Drift detection
    from terrapod.services.drift_detection_service import (
        handle_drift_run_completed,
    )

    # The completion handler must always be registered so manual "Check Now"
    # drift runs update workspace drift_status even when automatic polling
    # is disabled.
    register_trigger_handler(
        "drift_run_completed",
        handler=handle_drift_run_completed,
        description="Update workspace drift status on drift run completion",
    )

    # Periodic polling is only active when explicitly enabled.
    if settings.drift_detection.enabled:
        from terrapod.services.drift_detection_service import drift_check_cycle

        register_periodic_task(
            "drift_check",
            interval_seconds=settings.drift_detection.poll_interval_seconds,
            handler=drift_check_cycle,
            description="Check workspaces for infrastructure drift",
        )

    # AI plan summariser (#401). Always registered when feature is enabled
    # — runs that don't qualify (workspace disabled, budget exhausted) are
    # handled inside the handler so the trigger queue is uniform.
    if settings.ai_summary.enabled:
        from terrapod.services.summariser import handle_ai_plan_summary

        register_trigger_handler(
            "ai_plan_summary",
            handler=handle_ai_plan_summary,
            description="Summarise plan changes or analyse plan failures via LLM",
            # Its own lane: independent, I/O-bound model calls that a VCS poll
            # emits in bursts. Left in the default lane they were drained one
            # at a time behind (and in front of) sub-second status posts.
            lane=AI_LANE,
        )

        # AI cost narrative (#871) — the optional enhancement over the
        # deterministic cost estimate. Rides this same switch.
        from terrapod.services.cost_summariser import handle_ai_cost_summary

        register_trigger_handler(
            "ai_cost_summary",
            handler=handle_ai_cost_summary,
            description="Narrate a run's cost estimate + suggest savings via LLM",
            lane=AI_LANE,
        )

    # Run reconciler (drives run state transitions based on Job outcomes)
    from terrapod.services.run_reconciler import reconcile_runs

    register_periodic_task(
        "run_reconciler",
        interval_seconds=2,
        handler=reconcile_runs,
        description="Drive run state transitions based on Job outcomes",
    )

    # Bounded auto-retry for failed platform-initiated lifecycle destroys
    # (catalog + autodiscovery). Cheap no-op when there are none; the handler
    # self-gates to 0 retries / no eligible runs.
    from terrapod.services.lifecycle_destroy_retry import lifecycle_destroy_retry_cycle

    register_periodic_task(
        "lifecycle_destroy_retry",
        interval_seconds=30,
        handler=lifecycle_destroy_retry_cycle,
        description="Retry failed catalog/autodiscovery lifecycle destroy runs",
    )

    # Plan expiry sweep (#646): discard apply-capable planned runs that have aged
    # past their workspace's plan_expiry_seconds TTL. No-op when no workspace sets
    # a TTL (the default). Cheap query gated on plan_expiry_seconds > 0.
    from terrapod.services.run_service import expire_stale_plans_cycle

    register_periodic_task(
        "plan_expiry_sweep",
        interval_seconds=60,
        handler=expire_stale_plans_cycle,
        description="Discard planned runs past their workspace plan-expiry TTL",
    )

    # Audit log retention (daily)
    async def _audit_retention() -> None:
        from terrapod.services.audit_service import purge_old_entries

        async with get_db_session() as db:
            await purge_old_entries(db, settings.audit.retention_days)

    register_periodic_task(
        "audit_retention",
        interval_seconds=86400,  # daily
        handler=_audit_retention,
        description="Purge audit log entries older than retention period",
    )

    # Artifact retention cleanup (disabled by default)
    if settings.artifact_retention.enabled:

        async def _artifact_retention() -> None:
            from terrapod.services.artifact_retention_service import artifact_retention_cycle

            await artifact_retention_cycle()

        register_periodic_task(
            "artifact_retention",
            interval_seconds=settings.artifact_retention.poll_interval_seconds,
            handler=_artifact_retention,
            description="Clean up old artifacts from object storage",
        )

    # Abandoned OCI upload reaper. Every started-then-forgotten `docker push`
    # leaves a session row and its chunks behind, and only push access is needed
    # to do that repeatedly — so this is a storage-exhaustion control, not just
    # tidying. Hourly is ample against a timeout measured in hours.
    if settings.registry.oci.enabled:

        async def _oci_upload_reaper() -> None:
            from terrapod.services.oci.upload_service import reap_abandoned_sessions

            await reap_abandoned_sessions()

        register_periodic_task(
            "oci_upload_reaper",
            interval_seconds=3600,
            handler=_oci_upload_reaper,
            description="Reap abandoned OCI blob uploads and their chunks",
        )

    # Encryption DEK refresh — multi-replica DEK propagation (no leader election).
    # Lets a DEK rotated on one replica become usable on all replicas without a
    # restart. Cheap no-op when nothing changed. Only when encryption is enabled.
    if settings.encryption.enabled:

        async def _encryption_key_refresh() -> None:
            from terrapod.crypto.service import refresh_keys

            async with get_db_session() as db:
                await refresh_keys(db)

        register_periodic_task(
            "encryption_key_refresh",
            interval_seconds=30,
            handler=_encryption_key_refresh,
            description="Propagate rotated DEKs to all replicas (multi-replica safe)",
        )

    # Leadership probe (#960). Registered only under `ha.role=auto` — a static
    # role needs no probing at all, which is the overwhelmingly common case.
    #
    # This task must NOT be leadership-gated when the enforcement phase lands:
    # a follower has to keep probing or it can never discover that it has
    # become the leader.

    # A demotion by `helm upgrade --set api.config.ha.role=follower` — the step
    # the operations runbook prescribes — is only observable at startup, because
    # under a static role the probe below never runs. So the retirement
    # predicate has to be reached from here as well, or this node's in-flight
    # runs sit in `planning` for as long as it stays a follower (#1197). It is a
    # no-op on an ordinary restart that did not change the role.
    from terrapod.services.ha_role import reconcile_role_on_startup

    await reconcile_role_on_startup()

    if settings.ha.role == "auto":
        from terrapod.services.ha_role import probe_cycle

        register_periodic_task(
            "ha_probe",
            interval_seconds=settings.ha.probe_interval_seconds,
            handler=probe_cycle,
            description="Resolve this node's leader/follower role from DNS ownership",
        )

    # Settings replication (#960 phase 3, #1110). Both tasks are in the
    # scheduler's follower-safe set: the pull loop is the follower's entire
    # purpose, and the purge keeps the outbox bounded.
    from terrapod.services.replication_sync import purge_cycle, sync_cycle  # noqa: F401

    # Paired only (#1117) — a single-node install records no events, so there is
    # nothing to trim and no reason to run an hourly task. The purge is still
    # registered more broadly than the pull loop: BOTH nodes of a pair record
    # events (a follower tags its own with its origin so the two cannot echo),
    # so both need their outbox bounded, whereas only the follower pulls.
    if settings.ha.peer.url:
        register_periodic_task(
            "replication_purge",
            interval_seconds=3600,
            handler=purge_cycle,
            description="Trim replication outbox events beyond the retained window",
        )

    # In-cluster component readiness (#1122). Unlike replication this is useful
    # on ANY multi-replica deployment, pair or not — "am I serving from one
    # API pod" is an HA question a singleton operator also wants answered.
    if settings.ha.component_status.enabled:
        from terrapod.services.component_status import sample_cycle

        register_periodic_task(
            "component_status_sample",
            interval_seconds=settings.ha.component_status.interval_seconds,
            handler=sample_cycle,
            description="Sample in-cluster component readiness from Kubernetes",
        )

    if settings.ha.replication.enabled:
        register_periodic_task(
            "replication_sync",
            interval_seconds=settings.ha.replication.interval_seconds,
            handler=sync_cycle,
            description="Pull settings changes from the peer node",
        )

    # Object-store copying (#960 phase 4, #1159). Registered whenever any class
    # is set to `copy` — the default is `verify` everywhere, so on almost every
    # install this is not registered at all. Gated on the peer URL too: there is
    # nowhere to copy from without one, and registering a task that can only log
    # "no peer configured" every five minutes is noise.
    from terrapod.services import blob_classes

    if settings.ha.peer.url and any(
        blob_classes.effective_mode(c) == blob_classes.COPY for c in blob_classes.CLASSES
    ):
        from terrapod.services.blob_sync import sync_cycle as blob_sync_cycle

        register_periodic_task(
            "blob_sync",
            interval_seconds=settings.ha.blobs.interval_seconds,
            handler=blob_sync_cycle,
            description="Copy object-store classes marked `copy` from the peer node",
        )

    await start_scheduler()
    logger.info("Distributed scheduler started")

    # Slack integration (#556) — best-effort outbound Socket Mode connection.
    # Never fails startup: a misconfigured/disabled Slack just logs and skips.
    from terrapod.services.slack_service import start_slack

    await start_slack(settings)

    yield

    # Stop Slack connection
    from terrapod.services.slack_service import stop_slack

    await stop_slack()

    # Stop scheduler
    await stop_scheduler()
    logger.info("Distributed scheduler stopped")

    # Shutdown
    logger.info("Shutting down Terrapod API server")
    await close_storage()
    await close_redis()
    await close_db()


_REDOC_HTML = """<!DOCTYPE html>
<html><head>
<title>Terrapod API</title>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet"/>
<style>body{margin:0;padding:0;}</style>
</head><body>
<redoc spec-url="/api/openapi.json" theme='{
  "colors":{"primary":{"main":"#a78bfa"},"text":{"primary":"#e2e8f0","secondary":"#94a3b8"},
  "responses":{"success":{"color":"#4ade80","backgroundColor":"rgba(74,222,128,0.1)"},
  "error":{"color":"#f87171","backgroundColor":"rgba(248,113,113,0.1)"}},
  "http":{"get":"#4ade80","post":"#60a5fa","put":"#fbbf24","delete":"#f87171","patch":"#c084fc"}},
  "typography":{"fontSize":"14px","fontFamily":"Inter, sans-serif",
  "headings":{"fontFamily":"Inter, sans-serif","fontWeight":"700"},
  "code":{"fontSize":"13px","fontFamily":"JetBrains Mono, monospace","backgroundColor":"#1e293b"}},
  "sidebar":{"backgroundColor":"#0f172a","textColor":"#e2e8f0","activeTextColor":"#a78bfa",
  "groupItems":{"activeBackgroundColor":"#1e293b","activeTextColor":"#a78bfa","textColor":"#94a3b8"}},
  "rightPanel":{"backgroundColor":"#1e293b"},
  "schema":{"nestedBackground":"#0f172a","typeNameColor":"#a78bfa","labelsTextSize":"12px"}
}'></redoc>
<script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"></script>
</body></html>"""

_SWAGGER_HTML = """<!DOCTYPE html>
<html><head>
<title>Terrapod API</title>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"/>
<style>
body{margin:0;background:#0f172a;color:#e2e8f0;}
.swagger-ui{background:#0f172a;}
.swagger-ui .topbar{display:none;}
.swagger-ui .info .title,.swagger-ui .info .title small{color:#e2e8f0;}
.swagger-ui .info .description p,.swagger-ui .info .description,.swagger-ui .info li,
.swagger-ui .info a{color:#94a3b8;}
.swagger-ui .info a{color:#a78bfa;}
.swagger-ui .scheme-container{background:#1e293b;box-shadow:none;border-bottom:1px solid #334155;}
.swagger-ui .opblock-tag{color:#e2e8f0;border-bottom-color:#334155;}
.swagger-ui .opblock-tag:hover{color:#f1f5f9;}
.swagger-ui .opblock{border-color:#334155;background:rgba(30,41,59,0.5);}
.swagger-ui .opblock .opblock-summary{border-bottom-color:#334155;}
.swagger-ui .opblock .opblock-summary-description{color:#94a3b8;}
.swagger-ui .opblock .opblock-section-header{background:#1e293b;box-shadow:none;}
.swagger-ui .opblock .opblock-section-header h4{color:#e2e8f0;}
.swagger-ui .opblock-description-wrapper p,.swagger-ui .opblock-external-docs-wrapper p,
.swagger-ui table thead tr th,.swagger-ui table thead tr td,.swagger-ui .parameter__name,
.swagger-ui .parameter__type,.swagger-ui .response-col_status,.swagger-ui .response-col_description,
.swagger-ui label,.swagger-ui .btn{color:#e2e8f0;}
.swagger-ui .model-title,.swagger-ui .model{color:#e2e8f0;}
.swagger-ui .model-toggle::after{filter:invert(1);}
.swagger-ui section.models{border-color:#334155;}
.swagger-ui section.models .model-container{background:#1e293b;border-color:#334155;}
.swagger-ui .response-col_description__inner p{color:#94a3b8;}
.swagger-ui .btn.authorize{color:#a78bfa;border-color:#a78bfa;}
.swagger-ui .btn.authorize svg{fill:#a78bfa;}
.swagger-ui select{background:#1e293b;color:#e2e8f0;border-color:#334155;}
.swagger-ui input[type=text]{background:#1e293b;color:#e2e8f0;border-color:#334155;}
.swagger-ui .dialog-ux .modal-ux{background:#0f172a;border-color:#334155;}
.swagger-ui .dialog-ux .modal-ux-header h3{color:#e2e8f0;}
.swagger-ui .dialog-ux .modal-ux-content p{color:#94a3b8;}
.swagger-ui .model-box{background:#1e293b;}
.swagger-ui .prop-type{color:#a78bfa;}
.swagger-ui .renderedMarkdown p{color:#94a3b8;}
</style>
</head><body>
<div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url:"/api/openapi.json",dom_id:"#swagger-ui",
deepLinking:true,presets:[SwaggerUIBundle.presets.apis,SwaggerUIBundle.SwaggerUIStandalonePreset],
layout:"BaseLayout"});</script>
</body></html>"""


def create_application() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Terrapod API",
        description="Terrapod - Open-source Terraform Enterprise replacement",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # Custom themed API docs endpoints
    @app.get("/api/docs", include_in_schema=False)
    async def custom_swagger_ui() -> HTMLResponse:
        return HTMLResponse(_SWAGGER_HTML)

    @app.get("/api/redoc", include_in_schema=False)
    async def custom_redoc() -> HTMLResponse:
        return HTMLResponse(_REDOC_HTML)

    # Follower write gate (#1130). Registered FIRST, which makes it the
    # innermost user middleware: a refusal still passes back out through the
    # audit log, the security headers and the metrics counter on its way to the
    # client. Under the shipped `role: leader` default it is a config read that
    # always passes.
    from terrapod.api.follower_gate import follower_write_gate

    app.middleware("http")(follower_write_gate)

    # Rate limiting middleware (before metrics so 429 responses are counted)
    if settings.rate_limit.enabled:
        from terrapod.api.rate_limit import RateLimitMiddleware

        app.add_middleware(
            RateLimitMiddleware,
            requests_per_minute=settings.rate_limit.requests_per_minute,
            authenticated_requests_per_minute=settings.rate_limit.authenticated_requests_per_minute,
            runner_requests_per_minute=settings.rate_limit.runner_requests_per_minute,
            auth_requests_per_minute=settings.rate_limit.auth_requests_per_minute,
            distinct_credentials_per_minute=settings.rate_limit.distinct_credentials_per_minute,
        )

    # Prometheus metrics middleware + endpoint
    if settings.metrics.enabled:
        from terrapod.api.metrics import metrics_endpoint, metrics_middleware

        app.middleware("http")(metrics_middleware)
        app.add_api_route("/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False)

    # CORS middleware
    if settings.cors.allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors.allow_origins,
            allow_credentials=settings.cors.allow_credentials,
            allow_methods=settings.cors.allow_methods,
            allow_headers=settings.cors.allow_headers,
        )

    # Request ID middleware
    @app.middleware("http")
    async def add_request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Ensure every request has a request ID for logging correlation."""
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        structlog.contextvars.bind_contextvars(request_id=request_id)

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        structlog.contextvars.unbind_contextvars("request_id")

        return response

    # Security headers middleware
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Add security headers to every response."""
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Allow same-origin framing for built-in API docs (ReDoc, Swagger UI)
        if request.url.path in ("/api/docs", "/api/redoc"):
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
        else:
            response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

        # Propagate refreshed session expiry to frontend
        if hasattr(request.state, "session_expires_at"):
            response.headers["X-Session-Expires"] = request.state.session_expires_at

        return response

    # Audit logging middleware
    @app.middleware("http")
    async def audit_logging(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Log API requests to the audit log."""
        from terrapod.services.audit_service import (
            parse_resource,
            should_audit,
        )

        if not should_audit(request.url.path):
            return await call_next(request)

        start = time.monotonic()
        response = await call_next(request)
        duration_ms = int((time.monotonic() - start) * 1000)

        # Extract actor from request state (set by auth dependency) or response
        actor_email = ""
        if hasattr(request.state, "user_email"):
            actor_email = request.state.user_email

        actor_ip = request.client.host if request.client else ""
        request_id = response.headers.get("X-Request-ID", "")
        resource_type, resource_id = parse_resource(request.url.path)

        # Fire-and-forget: log asynchronously to avoid slowing down the response
        try:
            from terrapod.services.audit_service import log_audit_event

            async with get_db_session() as db:
                await log_audit_event(
                    db,
                    actor_email=actor_email,
                    actor_ip=actor_ip,
                    action=request.method,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    status_code=response.status_code,
                    request_id=request_id,
                    duration_ms=duration_ms,
                )
        except Exception:
            logger.warning("Failed to write audit log entry", exc_info=True)

        return response

    # Uncaught unique/constraint violations are a CONFLICT, not a server error.
    # Many create endpoints pre-check then INSERT, which races under multiple
    # replicas (the SELECT can't see a concurrent uncommitted INSERT); the loser
    # hits the unique constraint. get_db has already rolled the session back by
    # the time we get here. Hot paths still catch IntegrityError in-handler for a
    # specific message (e.g. catalog "name already exists"); this is the net so
    # the generic case is 409, not 500. Registered before the Exception handler
    # so the more specific type wins.
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import Response
    from fastapi.utils import is_body_allowed_for_status_code
    from sqlalchemy.exc import IntegrityError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from terrapod.api.errors import jsonapi_error_response
    from terrapod.services.ha_role import NotLeaderError

    # JSON:API house style (#1063): every error body carries a JSON:API
    # `errors` array AND the legacy top-level `detail`. Purely additive — old
    # clients keep reading `detail`; go-terrapod / JSON:API clients read
    # `errors`. Covers the ~740 `raise HTTPException(detail=…)` sites uniformly.
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        headers = getattr(exc, "headers", None)
        # Match FastAPI's default handler exactly for statuses that forbid a
        # body (1xx / 204 / 304): emit NO body. Without this the dual-key
        # envelope would put JSON on a 204/304, which is invalid HTTP and
        # differs from pre-#1063 behaviour. Nothing raises those today, but the
        # guard keeps this handler a strict superset of FastAPI's.
        if not is_body_allowed_for_status_code(exc.status_code):
            return Response(status_code=exc.status_code, headers=headers)
        return jsonapi_error_response(exc.detail, exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Keep FastAPI's 422 `detail` list verbatim; add the `errors` array.
        # `exc.errors()` can carry non-JSON-serialisable `ctx` values (Pydantic
        # v2), so run it through jsonable_encoder exactly as FastAPI's default
        # handler does before it reaches the response.
        from fastapi.encoders import jsonable_encoder

        return jsonapi_error_response(jsonable_encoder(exc.errors()), 422)

    @app.exception_handler(NotLeaderError)
    async def not_leader_handler(request: Request, exc: NotLeaderError) -> Response:
        """A write reached a node that does not currently hold the shared name.

        503 rather than 4xx: the request is well-formed and the caller is
        authorised — this node simply is not the one serving writes. Retry
        against whoever holds the name.
        """
        logger.info("Write refused: not the leader", action=exc.action, path=request.url.path)
        return jsonapi_error_response(str(exc), 503)

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
        logger.warning(
            "Integrity constraint violation",
            path=str(request.url.path),
            error=str(getattr(exc, "orig", exc)),
        )
        return jsonapi_error_response("Resource already exists or violates a constraint", 409)

    @app.exception_handler(httpx.HTTPError)
    async def upstream_error_handler(request: Request, exc: httpx.HTTPError) -> JSONResponse:
        """An upstream provider failed, so say which one and how.

        Reaching the catch-all below turned every VCS outage into a bare
        "Internal server error" — during a GitHub incident, queueing a run
        returned 500 with nothing to distinguish "GitHub is broken" from "you
        misconfigured this workspace" or "Terrapod has a bug". 502 rather than
        500 is the honest answer: the request was fine and we are the gateway.

        This is the backstop, not the explanation. A handler that knows what it
        was doing should catch the failure itself and say so in the caller's
        terms; this exists so that the ones which don't cannot present someone
        else's outage as ours.
        """
        status = 502
        upstream = ""
        request_url = getattr(exc, "request", None)
        if request_url is not None:
            # Host and path only. A redirected archive download can carry a
            # credential in the query string, and this string is returned to
            # the caller and written to the log.
            url = request_url.url
            upstream = f"{url.host}{url.path}"
        if isinstance(exc, httpx.TimeoutException):
            status = 504
            detail = f"Upstream service timed out ({upstream or 'unknown host'})"
        elif isinstance(exc, httpx.HTTPStatusError):
            detail = (
                f"Upstream service returned HTTP {exc.response.status_code} "
                f"({upstream or 'unknown host'})"
            )
        else:
            detail = f"Could not reach upstream service ({upstream or 'unknown host'})"

        logger.warning(
            "Upstream provider error",
            path=str(request.url.path),
            upstream=upstream,
            status=status,
            error=str(exc),
        )
        return jsonapi_error_response(detail, status, headers={UPSTREAM_FAILURE_HEADER: "upstream"})

    # Global exception handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Global exception handler for unhandled errors."""
        logger.error("Unhandled exception", exc_info=exc, path=str(request.url.path))
        return jsonapi_error_response("Internal server error", 500)

    # ── API prefix conventions ──────────────────────────────────────
    #
    # Terrapod has two distinct API namespaces:
    #
    # * `/api/v2/` — TFE V2 compatibility surface only. Paths must match
    #   the official HCP Terraform / TFE V2 spec so go-tfe / terraform CLI
    #   work unchanged. Permanent home; never deprecated wholesale.
    #
    # * `/api/terrapod/v1/` — Terrapod-specific extensions (admin,
    #   labels, roles, audit, listener protocol, runner artifacts, auth
    #   sessions, etc.). All Terrapod-only endpoints live here from
    #   v0.23.0 onward.
    #
    # The legacy `/api/v2/` aliases for Terrapod-only routes (the
    # transitional dual-mount #269 introduced and the v0.23.x release
    # window kept) were removed in v0.24.0 — see #278. Terrapod-native
    # endpoints are served *only* at `/api/terrapod/v1/`. `/api/v2/`
    # remains the permanent home for the TFE V2 CLI surface that
    # `terraform` / `tofu` / `tfci` consume (see docs/tfe-cli-surface.md);
    # that is not deprecated and is unaffected by #278.
    TERRAPOD_PREFIX = "/api/terrapod/v1"

    def include_terrapod(router) -> None:
        """Mount a Terrapod-native router at the canonical
        `/api/terrapod/v1/` prefix. Any prefix on the router itself
        stacks (e.g. audit's own `prefix="/admin"` becomes
        `/api/terrapod/v1/admin/...`).
        """
        app.include_router(router, prefix=TERRAPOD_PREFIX)

    # Health endpoints (no prefix)
    app.include_router(health_router)

    # Filesystem storage routes (presigned URL handlers) — Terrapod-only
    # dev backend. Canonical at /api/terrapod/v1; filesystem.py emits
    # presigned URLs under that prefix.
    from terrapod.storage.filesystem_routes import router as fs_router

    include_terrapod(fs_router)

    # Auth routes — Terrapod-specific session/SSO management.
    from terrapod.api.routers.auth import router as auth_router

    include_terrapod(auth_router)

    # OAuth2 routes (terraform login flow). The OAuth + service-discovery
    # paths stay at their canonical locations (/.well-known/terraform.json,
    # /oauth/*) — those are external standards, not Terrapod-versioned.
    # The Terrapod-only cli-login-status check moves to /api/terrapod/v1.
    from terrapod.api.routers.oauth import (
        extensions_router as oauth_extensions_router,
    )
    from terrapod.api.routers.oauth import (
        router as oauth_router,
    )

    app.include_router(oauth_router)
    include_terrapod(oauth_extensions_router)

    # Workspace extension routes (SSE, vcs-refs) — Terrapod-specific.
    # MUST come before tfe_v2 so /workspace-events isn't matched as a
    # workspace_id parameter on either prefix.
    from terrapod.api.routers.workspace_extensions import router as workspace_extensions_router

    include_terrapod(workspace_extensions_router)

    # TFE V2 CLI-contract routes — the verified subset of the TFE V2 spec
    # that terraform/tofu/tfci consume (see docs/tfe-cli-surface.md).
    # The one workspace-management path the CLI doesn't call (DELETE by
    # id) lives in extensions_router, mounted only under /api/terrapod/v1.
    from terrapod.api.routers.tfe_v2 import (
        extensions_router as tfe_v2_extensions_router,
    )
    from terrapod.api.routers.tfe_v2 import (
        router as tfe_v2_router,
    )

    app.include_router(tfe_v2_router)
    include_terrapod(tfe_v2_extensions_router)

    # State management routes — Terrapod-specific (delete, rollback, upload).
    from terrapod.api.routers.state_management import router as state_management_router

    include_terrapod(state_management_router)

    # Token CRUD routes — Terrapod-native management surface (the CLI
    # creates tokens via the /oauth flow, never via these endpoints).
    from terrapod.api.routers.tokens import router as tokens_router

    include_terrapod(tokens_router)

    # Registry routes — module CLI download protocol stays at /api/v2 (the
    # CLI hits this on `terraform init`). Module management (private-module
    # CRUD + version + /vcs) and workspace-links are Terrapod-native and
    # mounted only under /api/terrapod/v1.
    from terrapod.api.routers.registry_modules import (
        management_router as registry_modules_management_router,
    )
    from terrapod.api.routers.registry_modules import (
        router as registry_modules_router,
    )
    from terrapod.api.routers.registry_modules import (
        workspace_links_router as module_workspace_links_router,
    )

    app.include_router(registry_modules_router)
    include_terrapod(registry_modules_management_router)
    include_terrapod(module_workspace_links_router)

    # Provider registry — CLI download protocol stays at /api/v2; provider
    # management is Terrapod-native under /api/terrapod/v1.
    from terrapod.api.routers.registry_providers import (
        management_router as registry_providers_management_router,
    )
    from terrapod.api.routers.registry_providers import (
        router as registry_providers_router,
    )

    app.include_router(registry_providers_router)
    include_terrapod(registry_providers_management_router)

    # GPG keys — Terrapod-native (the CLI reads provider GPG keys from the
    # provider download response, not via this admin endpoint). Canonical
    # under /api/terrapod/v1; the historical TFE path
    # /api/registry/private/v2/gpg-keys was removed in v0.24.0 (#278).
    from terrapod.api.routers.gpg_keys import router as gpg_keys_router

    include_terrapod(gpg_keys_router)

    # Caching routes (provider mirror, binary cache)
    from terrapod.api.routers.provider_mirror import router as provider_mirror_router

    app.include_router(provider_mirror_router)

    # OCI Distribution registry (#1408). Root-mounted at /v2/ because the spec
    # mandates that prefix; its exception handler is registered on the app so no
    # route can accidentally answer a container client in the house error shape.
    from terrapod.api.routers.oci import oci_error_handler
    from terrapod.api.routers.oci import router as oci_router
    from terrapod.services.oci.errors import OCIError

    app.include_router(oci_router)
    app.add_exception_handler(OCIError, oci_error_handler)

    from terrapod.api.routers.binary_cache import router as binary_cache_router

    include_terrapod(binary_cache_router)

    # Cost-estimation pricesheet cache (#871) — runner-facing download + admin.
    from terrapod.api.routers.cost_estimation import router as cost_estimation_router

    include_terrapod(cost_estimation_router)

    # Variable endpoints
    from terrapod.api.routers.variables import router as variables_router

    app.include_router(variables_router)

    # Agent pool endpoints — Terrapod-native management (pool CRUD,
    # token CRUD, listener-protocol). The CLI never manages pools, so
    # canonical paths drop the /organizations/default/ segment (Terrapod
    # is single-org — see CLAUDE.md rule #9).
    from terrapod.api.routers.agent_pools import (
        listener_router as listener_protocol_router,
    )
    from terrapod.api.routers.agent_pools import (
        router as agent_pools_router,
    )

    app.include_router(agent_pools_router, prefix=TERRAPOD_PREFIX)
    include_terrapod(listener_protocol_router)

    # Read-only labels browser (cross-entity: workspaces, pools, modules, providers).
    from terrapod.api.routers.labels import router as labels_router

    include_terrapod(labels_router)

    # Run endpoints — TFE-spec stays at /api/v2; Terrapod-only extensions
    # (listener protocol, runner-driven completion, SSE streams, retry)
    # are Terrapod-native under /api/terrapod/v1.
    from terrapod.api.routers.runs import (
        extensions_router as runs_extensions_router,
    )
    from terrapod.api.routers.runs import (
        router as runs_router,
    )

    app.include_router(runs_router)
    include_terrapod(runs_extensions_router)

    # Run artifact endpoints (runner token auth) — Terrapod runner protocol.
    from terrapod.api.routers.run_artifacts import router as run_artifacts_router

    include_terrapod(run_artifacts_router)

    # Configuration version endpoints — TFE-spec stays at /api/v2; the
    # Terrapod download/diff/ticket extensions are Terrapod-native under
    # /api/terrapod/v1.
    from terrapod.api.routers.config_versions import (
        extensions_router as config_version_extensions_router,
    )
    from terrapod.api.routers.config_versions import (
        router as config_versions_router,
    )

    app.include_router(config_versions_router)
    include_terrapod(config_version_extensions_router)

    # VCS connection endpoints — Terrapod-native. Canonical paths at
    # /api/terrapod/v1/vcs-connections{,/{id}}.
    from terrapod.api.routers.vcs_connections import (
        router as vcs_connections_router,
    )

    app.include_router(vcs_connections_router, prefix=TERRAPOD_PREFIX)

    # Autodiscovery rules — Terrapod-native, introduced in v0.24 (#283).
    # No legacy alias: this surface didn't exist in v0.22, so /api/v2 has
    # nothing to preserve.
    from terrapod.api.routers.autodiscovery_rules import (
        router as autodiscovery_rules_router,
    )

    app.include_router(autodiscovery_rules_router, prefix=TERRAPOD_PREFIX)

    # Bulk workspace operations — Terrapod-native admin (#318): search +
    # all-or-nothing bulk-update of fields/run-tasks/notifications.
    from terrapod.api.routers.workspace_bulk import router as workspace_bulk_router

    app.include_router(workspace_bulk_router, prefix=TERRAPOD_PREFIX)

    # Service catalog (#535): provider-template + catalog-item management +
    # provision flow. The router self-gates on settings.catalog.enabled (404
    # when disabled), so it is always mounted.
    from terrapod.api.routers.catalog import router as catalog_router

    app.include_router(catalog_router, prefix=TERRAPOD_PREFIX)

    # AI onboarding (#824) — discover existing resources → copy-pasteable
    # resource + import blocks. Self-gates on settings.ai_onboarding.enabled
    # (404 when disabled), so it is always mounted.
    from terrapod.api.routers.onboarding import router as onboarding_router

    app.include_router(onboarding_router, prefix=TERRAPOD_PREFIX)

    # VCS webhook event receiver — Terrapod-specific.
    from terrapod.api.routers.vcs_events import router as vcs_events_router

    include_terrapod(vcs_events_router)

    # Role CRUD — Terrapod-specific RBAC.
    from terrapod.api.routers.roles import router as roles_router

    include_terrapod(roles_router)

    # Role assignment management — Terrapod-specific RBAC.
    from terrapod.api.routers.role_assignments import router as role_assignments_router

    include_terrapod(role_assignments_router)

    # Run trigger endpoints — Terrapod-native management (CLI doesn't use).
    from terrapod.api.routers.run_triggers import router as run_triggers_router

    include_terrapod(run_triggers_router)

    # Execution hooks — Terrapod-native library of custom-shell steps run in the
    # runner Job at fixed points (#619). Admin-managed; associated to workspaces.
    from terrapod.api.routers.execution_hooks import (
        router as execution_hooks_router,
    )

    include_terrapod(execution_hooks_router)

    # Slack account-linking — bind a Slack identity to a Terrapod identity (#556).
    from terrapod.api.routers.slack import router as slack_router

    include_terrapod(slack_router)

    # Remote-state consumer allowlist — Terrapod-native management of the
    # producer-controlled cross-workspace `terraform_remote_state` grants
    # (#344). The CLI never manages these; the read-path authorization
    # consuming the allowlist lives in tfe_v2.py on the existing
    # CLI-contract endpoints.
    from terrapod.api.routers.remote_state_consumers import (
        router as remote_state_consumers_router,
    )

    include_terrapod(remote_state_consumers_router)

    # OPA policy-as-code enforcement — Terrapod-native management of
    # policy sets + policies, plus per-run policy evaluations and the
    # admin override action (#343).
    from terrapod.api.routers.policy_sets import router as policy_sets_router

    include_terrapod(policy_sets_router)

    # Security scanning (#1036): deterministic Checkov/Trivy IaC-misconfig scan
    # stage — runner config/results + run-read + admin override.
    from terrapod.api.routers.security_scanning import router as security_scanning_router

    include_terrapod(security_scanning_router)

    # Audit log query endpoint — Terrapod-specific.
    from terrapod.api.routers.audit import router as audit_router

    include_terrapod(audit_router)

    # Encryption-at-rest status (admin only) — Terrapod-specific (#553).
    from terrapod.api.routers.encryption import router as encryption_router

    include_terrapod(encryption_router)

    # Undelete surface for deleted workspaces (admin only) — Terrapod-specific
    # (#1253). Not a TFE concept; there is no CLI client for it.
    from terrapod.api.routers.deleted_workspaces import router as deleted_workspaces_router

    include_terrapod(deleted_workspaces_router)

    # Node identity + current role, for leader/follower resolution (#960).
    # Unauthenticated by necessity: the probe runs before trust exists between
    # nodes and discloses only an operator-chosen name and a role.
    from terrapod.api.routers.ha import router as ha_router

    include_terrapod(ha_router)

    # Peer replication reads (#960 phase 3, #1110). Gated on `get_peer_identity`,
    # which accepts a `peer` token and which nothing else accepts.
    from terrapod.api.routers.replication import router as replication_router

    include_terrapod(replication_router)

    # User management endpoints — Terrapod-native. Canonical paths at
    # /api/terrapod/v1/users{,/{email}}.
    from terrapod.api.routers.users import (
        router as users_router,
    )

    app.include_router(users_router, prefix=TERRAPOD_PREFIX)

    # Notification configuration endpoints — Terrapod-native management.
    from terrapod.api.routers.notification_configurations import (
        router as notification_configurations_router,
    )

    include_terrapod(notification_configurations_router)

    # Run task endpoints — task-stages (read + override) stay at /api/v2
    # because the CLI's cloud backend reads them on every run; everything
    # else (workspace-scoped task definition CRUD + callback receiver) is
    # Terrapod-native under /api/terrapod/v1.
    from terrapod.api.routers.run_tasks import (
        extensions_router as run_tasks_extensions_router,
    )
    from terrapod.api.routers.run_tasks import (
        router as run_tasks_router,
    )

    app.include_router(run_tasks_router)
    include_terrapod(run_tasks_extensions_router)

    return app


# Application instance
app = create_application()
