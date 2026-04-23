"""
FastAPI application factory for Terrapod API server.

Uses lifespan handler for startup/shutdown with async resource management.
"""

import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

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

    await init_redis()
    logger.info("Redis initialized")

    init_connectors()
    logger.info("Auth connectors initialized")

    await init_storage()
    logger.info("Storage initialized")

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
        from terrapod.services.registry_vcs_poller import registry_vcs_poll_cycle

        register_periodic_task(
            "registry_vcs_poll",
            interval_seconds=settings.vcs.poll_interval_seconds,
            handler=registry_vcs_poll_cycle,
            description="Poll VCS providers for new module version tags",
        )

        # Module impact analysis: speculative plans for module PRs
        from terrapod.services.module_impact_service import (
            handle_module_impact_immediate_poll,
            handle_module_test_completed,
            module_impact_poll_cycle,
        )

        register_periodic_task(
            "module_impact_poll",
            interval_seconds=settings.vcs.poll_interval_seconds,
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

    # Notification delivery handler (always registered)
    from terrapod.services.notification_dispatcher import handle_notification_delivery

    register_trigger_handler(
        "notification_deliver",
        handler=handle_notification_delivery,
        description="Deliver workspace notification on run state change",
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

    # Run reconciler (drives run state transitions based on Job outcomes)
    from terrapod.services.run_reconciler import reconcile_runs

    register_periodic_task(
        "run_reconciler",
        interval_seconds=2,
        handler=reconcile_runs,
        description="Drive run state transitions based on Job outcomes",
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

    await start_scheduler()
    logger.info("Distributed scheduler started")

    yield

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

    # Rate limiting middleware (before metrics so 429 responses are counted)
    if settings.rate_limit.enabled:
        from terrapod.api.rate_limit import RateLimitMiddleware

        app.add_middleware(
            RateLimitMiddleware,
            requests_per_minute=settings.rate_limit.requests_per_minute,
            authenticated_requests_per_minute=settings.rate_limit.authenticated_requests_per_minute,
            auth_requests_per_minute=settings.rate_limit.auth_requests_per_minute,
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

    # Global exception handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Global exception handler for unhandled errors."""
        logger.error("Unhandled exception", exc_info=exc, path=str(request.url.path))
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    # Health endpoints (no prefix)
    app.include_router(health_router)

    # Filesystem storage routes (presigned URL handlers)
    from terrapod.storage.filesystem_routes import router as fs_router

    app.include_router(fs_router, prefix=settings.api_prefix)

    # Auth routes
    from terrapod.api.routers.auth import router as auth_router

    app.include_router(auth_router, prefix=settings.api_prefix)

    # OAuth2 routes (terraform login flow)
    from terrapod.api.routers.oauth import router as oauth_router

    app.include_router(oauth_router)

    # Workspace extension routes (SSE, vcs-refs — must come before tfe_v2
    # so /workspace-events is not matched as a workspace_id parameter)
    from terrapod.api.routers.workspace_extensions import router as workspace_extensions_router

    app.include_router(workspace_extensions_router)

    # TFE V2 compatibility routes
    from terrapod.api.routers.tfe_v2 import router as tfe_v2_router

    app.include_router(tfe_v2_router)

    # State management routes (Terrapod-specific: delete, rollback, upload)
    from terrapod.api.routers.state_management import router as state_management_router

    app.include_router(state_management_router)

    # Token CRUD routes
    from terrapod.api.routers.tokens import router as tokens_router

    app.include_router(tokens_router)

    # Registry routes (modules, providers, GPG keys)
    from terrapod.api.routers.registry_modules import router as registry_modules_router

    app.include_router(registry_modules_router)

    from terrapod.api.routers.registry_providers import router as registry_providers_router

    app.include_router(registry_providers_router)

    from terrapod.api.routers.gpg_keys import router as gpg_keys_router

    app.include_router(gpg_keys_router)

    # Caching routes (provider mirror, binary cache)
    from terrapod.api.routers.provider_mirror import router as provider_mirror_router

    app.include_router(provider_mirror_router)

    from terrapod.api.routers.binary_cache import router as binary_cache_router

    app.include_router(binary_cache_router)

    # Variable endpoints
    from terrapod.api.routers.variables import router as variables_router

    app.include_router(variables_router)

    # Agent pool endpoints
    from terrapod.api.routers.agent_pools import router as agent_pools_router

    app.include_router(agent_pools_router)

    # Run endpoints
    from terrapod.api.routers.runs import router as runs_router

    app.include_router(runs_router)

    # Run artifact endpoints (runner token auth)
    from terrapod.api.routers.run_artifacts import router as run_artifacts_router

    app.include_router(run_artifacts_router)

    # Configuration version endpoints
    from terrapod.api.routers.config_versions import router as config_versions_router

    app.include_router(config_versions_router)

    # VCS connection endpoints
    from terrapod.api.routers.vcs_connections import router as vcs_connections_router

    app.include_router(vcs_connections_router)

    # VCS webhook event receiver
    from terrapod.api.routers.vcs_events import router as vcs_events_router

    app.include_router(vcs_events_router)

    # Role CRUD
    from terrapod.api.routers.roles import router as roles_router

    app.include_router(roles_router)

    # Role assignment management
    from terrapod.api.routers.role_assignments import router as role_assignments_router

    app.include_router(role_assignments_router)

    # Run trigger endpoints
    from terrapod.api.routers.run_triggers import router as run_triggers_router

    app.include_router(run_triggers_router)

    # Audit log query endpoint
    from terrapod.api.routers.audit import router as audit_router

    app.include_router(audit_router)

    # User management endpoints
    from terrapod.api.routers.users import router as users_router

    app.include_router(users_router)

    # Notification configuration endpoints
    from terrapod.api.routers.notification_configurations import (
        router as notification_configurations_router,
    )

    app.include_router(notification_configurations_router)

    # Run task endpoints
    from terrapod.api.routers.run_tasks import router as run_tasks_router

    app.include_router(run_tasks_router)

    return app


# Application instance
app = create_application()
