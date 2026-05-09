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
            runner_requests_per_minute=settings.rate_limit.runner_requests_per_minute,
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

    # Deprecation-headers middleware. Stamps RFC 8594 Deprecation + Link
    # headers on responses for routes flagged `deprecated=True` in the
    # OpenAPI schema, plus a custom X-Removed-In header naming the version
    # that drops the alias (RFC 8594's Sunset wants an HTTP-date which
    # we'd rather not commit to for a release-train product).
    #
    # Also emits a structlog warning per legacy-path request so operators
    # can grep their logs for any client still pointed at the old paths
    # before the v0.24.0 cutover (see issues #269 + #278).
    @app.middleware("http")
    async def deprecation_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        route = request.scope.get("route")
        if route is not None and getattr(route, "deprecated", False):
            response.headers["Deprecation"] = "true"
            response.headers["Link"] = (
                '<https://github.com/mattrobinsonsre/terrapod/issues/278>; rel="deprecation"'
            )
            response.headers["X-Removed-In"] = "v0.24.0"
            logger.warning(
                "Deprecated API path used",
                path=request.url.path,
                method=request.method,
                user_agent=request.headers.get("user-agent", ""),
            )
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
    # During the v0.23.x deprecation window every Terrapod-only route is
    # ALSO registered at its original `/api/v2/` location with FastAPI's
    # `deprecated=True` flag — this lets v0.X-1 listeners/runners/the
    # provider still hit a v0.X API while operators stagger upgrades.
    # The legacy mounts get dropped in v0.24.0 (see #278). Internal code
    # always targets the canonical `/api/terrapod/v1/` path.
    TERRAPOD_PREFIX = "/api/terrapod/v1"
    TFE_PREFIX = settings.api_prefix  # "/api/v2"

    def include_with_org_legacy(router) -> None:
        """Register a Terrapod-native router that pre-v0.23 lived under
        `/api/v2/organizations/default/{resource}`.

        Two mounts:
        - Canonical: `/api/terrapod/v1/{resource}` (no
          `organizations/default/` — see CLAUDE.md rule #9 and
          `docs/tfe-cli-surface.md`).
        - Legacy alias: `/api/v2/organizations/default/{resource}`
          (the pre-v0.23 path, kept for v0.22.x callers).

        We deliberately do NOT register a `/api/v2/{resource}` alias —
        that shape was never published, isn't a TFE-V2 shape, and isn't
        something we want to preserve. The router's own `prefix=` and
        each route's path stack on these prefixes as usual.

        Legacy mount is hidden from OpenAPI and emits Deprecation
        headers via the middleware. Removed in v0.24.0 (#278).
        """
        app.include_router(router, prefix=TERRAPOD_PREFIX)
        app.include_router(
            router,
            prefix="/api/v2/organizations/default",
            deprecated=True,
            include_in_schema=False,
        )

    def include_moved(router, legacy_prefix: str = TFE_PREFIX) -> None:
        """Register a router under both the canonical Terrapod prefix and
        a legacy alias.

        `legacy_prefix` defaults to `/api/v2/` since that's where every
        TFE-V2-shaped route used to live; pass a different prefix for
        routes that historically used a non-`/api/v2/` path (e.g. GPG
        keys lived at `/api/registry/private/v2/`).

        The legacy mount is `deprecated=True` (so the middleware emits
        Deprecation/Link/X-Removed-In response headers + a structlog
        warning per legacy-path request) AND `include_in_schema=False`
        so the alias paths are hidden from OpenAPI / Swagger UI / ReDoc /
        generated clients. The canonical `/api/terrapod/v1/...` paths
        are the only ones documented; the alias is a transitional
        compatibility-only mount.

        Any prefix on the router itself stacks (e.g. audit's own
        `prefix="/admin"` becomes `/api/terrapod/v1/admin/...` and
        `/api/v2/admin/...`).
        """
        app.include_router(router, prefix=TERRAPOD_PREFIX)
        app.include_router(
            router,
            prefix=legacy_prefix,
            deprecated=True,
            include_in_schema=False,
        )

    # Health endpoints (no prefix)
    app.include_router(health_router)

    # Filesystem storage routes (presigned URL handlers) — Terrapod-only
    # dev backend. Dual-mounted: filesystem.py emits canonical
    # /api/terrapod/v1 URLs for new presigns; the /api/v2 alias keeps
    # any URLs already in flight (or generated by older replicas during
    # rollout) resolvable until v0.24.0 (#278).
    from terrapod.storage.filesystem_routes import router as fs_router

    include_moved(fs_router)

    # Auth routes — Terrapod-specific session/SSO management.
    from terrapod.api.routers.auth import router as auth_router

    include_moved(auth_router)

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
    include_moved(oauth_extensions_router)

    # Workspace extension routes (SSE, vcs-refs) — Terrapod-specific.
    # MUST come before tfe_v2 so /workspace-events isn't matched as a
    # workspace_id parameter on either prefix.
    from terrapod.api.routers.workspace_extensions import router as workspace_extensions_router

    include_moved(workspace_extensions_router)

    # TFE V2 CLI-contract routes — the verified subset of the TFE V2 spec
    # that terraform/tofu/tfci consume (see docs/tfe-cli-surface.md).
    # The one workspace-management path the CLI doesn't call (DELETE by
    # id) lives in extensions_router and dual-mounts under /api/terrapod/v1.
    from terrapod.api.routers.tfe_v2 import (
        extensions_router as tfe_v2_extensions_router,
    )
    from terrapod.api.routers.tfe_v2 import (
        router as tfe_v2_router,
    )

    app.include_router(tfe_v2_router)
    include_moved(tfe_v2_extensions_router)

    # State management routes — Terrapod-specific (delete, rollback, upload).
    from terrapod.api.routers.state_management import router as state_management_router

    include_moved(state_management_router)

    # Token CRUD routes — Terrapod-native management surface (the CLI
    # creates tokens via the /oauth flow, never via these endpoints).
    from terrapod.api.routers.tokens import router as tokens_router

    include_moved(tokens_router)

    # Registry routes — module CLI download protocol stays at /api/v2 (the
    # CLI hits this on `terraform init`). Module management (org-scoped CRUD
    # on private modules + version + /vcs) and workspace-links are
    # Terrapod-native and dual-mount under /api/terrapod/v1.
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
    include_with_org_legacy(registry_modules_management_router)
    include_with_org_legacy(module_workspace_links_router)

    # Provider registry — CLI download protocol stays at /api/v2; org-scoped
    # management dual-mounts under /api/terrapod/v1.
    from terrapod.api.routers.registry_providers import (
        management_router as registry_providers_management_router,
    )
    from terrapod.api.routers.registry_providers import (
        router as registry_providers_router,
    )

    app.include_router(registry_providers_router)
    include_with_org_legacy(registry_providers_management_router)

    # GPG keys — Terrapod-native (the CLI reads provider GPG keys from the
    # provider download response, not via this admin endpoint). Canonical
    # under /api/terrapod/v1; the historical TFE path /api/registry/private/v2
    # is dual-mounted as the deprecated alias.
    from terrapod.api.routers.gpg_keys import router as gpg_keys_router

    include_moved(gpg_keys_router, legacy_prefix="/api/registry/private/v2")

    # Caching routes (provider mirror, binary cache)
    from terrapod.api.routers.provider_mirror import router as provider_mirror_router

    app.include_router(provider_mirror_router)

    from terrapod.api.routers.binary_cache import router as binary_cache_router

    include_moved(binary_cache_router)

    # Variable endpoints
    from terrapod.api.routers.variables import router as variables_router

    app.include_router(variables_router)

    # Agent pool endpoints — Terrapod-native management (pool CRUD,
    # token CRUD, listener-protocol). The CLI never manages pools, so
    # canonical paths drop the /organizations/default/ segment (Terrapod
    # is single-org — see CLAUDE.md rule #9). Pre-v0.23 path shapes are
    # preserved as deprecated aliases via legacy_router (mounted only on
    # /api/v2).
    from terrapod.api.routers.agent_pools import (
        legacy_router as agent_pools_legacy_router,
    )
    from terrapod.api.routers.agent_pools import (
        listener_router as listener_protocol_router,
    )
    from terrapod.api.routers.agent_pools import (
        router as agent_pools_router,
    )

    app.include_router(agent_pools_router, prefix=TERRAPOD_PREFIX)
    app.include_router(
        agent_pools_legacy_router,
        prefix=TFE_PREFIX,
        deprecated=True,
        include_in_schema=False,
    )
    include_moved(listener_protocol_router)

    # Read-only labels browser (cross-entity: workspaces, pools, modules, providers).
    from terrapod.api.routers.labels import router as labels_router

    include_moved(labels_router)

    # Run endpoints — TFE-spec stays at /api/v2; Terrapod-only extensions
    # (listener protocol, runner-driven completion, SSE streams, retry)
    # move to /api/terrapod/v1 with a deprecated /api/v2 alias.
    from terrapod.api.routers.runs import (
        extensions_router as runs_extensions_router,
    )
    from terrapod.api.routers.runs import (
        router as runs_router,
    )

    app.include_router(runs_router)
    include_moved(runs_extensions_router)

    # Run artifact endpoints (runner token auth) — Terrapod runner protocol.
    from terrapod.api.routers.run_artifacts import router as run_artifacts_router

    include_moved(run_artifacts_router)

    # Configuration version endpoints — TFE-spec stays at /api/v2; the
    # Terrapod download/diff/ticket extensions move to /api/terrapod/v1.
    from terrapod.api.routers.config_versions import (
        extensions_router as config_version_extensions_router,
    )
    from terrapod.api.routers.config_versions import (
        router as config_versions_router,
    )

    app.include_router(config_versions_router)
    include_moved(config_version_extensions_router)

    # VCS connection endpoints — Terrapod-native. Canonical paths at
    # /api/terrapod/v1/vcs-connections{,/{id}}; legacy_router preserves
    # the pre-v0.23 list/create at /api/v2/organizations/default/vcs-connections
    # and by-id at /api/v2/vcs-connections/{id}.
    from terrapod.api.routers.vcs_connections import (
        legacy_router as vcs_connections_legacy_router,
    )
    from terrapod.api.routers.vcs_connections import (
        router as vcs_connections_router,
    )

    app.include_router(vcs_connections_router, prefix=TERRAPOD_PREFIX)
    app.include_router(
        vcs_connections_legacy_router,
        prefix=TFE_PREFIX,
        deprecated=True,
        include_in_schema=False,
    )

    # Autodiscovery rules — Terrapod-native, introduced in v0.24 (#283).
    # No legacy alias: this surface didn't exist in v0.22, so /api/v2 has
    # nothing to preserve.
    from terrapod.api.routers.autodiscovery_rules import (
        router as autodiscovery_rules_router,
    )

    app.include_router(autodiscovery_rules_router, prefix=TERRAPOD_PREFIX)

    # VCS webhook event receiver — Terrapod-specific.
    from terrapod.api.routers.vcs_events import router as vcs_events_router

    include_moved(vcs_events_router)

    # Role CRUD — Terrapod-specific RBAC.
    from terrapod.api.routers.roles import router as roles_router

    include_moved(roles_router)

    # Role assignment management — Terrapod-specific RBAC.
    from terrapod.api.routers.role_assignments import router as role_assignments_router

    include_moved(role_assignments_router)

    # Run trigger endpoints — Terrapod-native management (CLI doesn't use).
    from terrapod.api.routers.run_triggers import router as run_triggers_router

    include_moved(run_triggers_router)

    # Audit log query endpoint — Terrapod-specific.
    from terrapod.api.routers.audit import router as audit_router

    include_moved(audit_router)

    # User management endpoints — Terrapod-native. Canonical paths at
    # /api/terrapod/v1/users{,/{email}}; legacy_router preserves
    # pre-v0.23 list/create at /api/v2/organizations/default/users and
    # by-email at /api/v2/users/{email}.
    from terrapod.api.routers.users import (
        legacy_router as users_legacy_router,
    )
    from terrapod.api.routers.users import (
        router as users_router,
    )

    app.include_router(users_router, prefix=TERRAPOD_PREFIX)
    app.include_router(
        users_legacy_router,
        prefix=TFE_PREFIX,
        deprecated=True,
        include_in_schema=False,
    )

    # Notification configuration endpoints — Terrapod-native management.
    from terrapod.api.routers.notification_configurations import (
        router as notification_configurations_router,
    )

    include_moved(notification_configurations_router)

    # Run task endpoints — task-stages (read + override) stay at /api/v2
    # because the CLI's cloud backend reads them on every run; everything
    # else (workspace-scoped task definition CRUD + callback receiver) is
    # Terrapod-native and dual-mounts under /api/terrapod/v1.
    from terrapod.api.routers.run_tasks import (
        extensions_router as run_tasks_extensions_router,
    )
    from terrapod.api.routers.run_tasks import (
        router as run_tasks_router,
    )

    app.include_router(run_tasks_router)
    include_moved(run_tasks_extensions_router)

    return app


# Application instance
app = create_application()
