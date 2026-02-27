"""
FastAPI application factory for Terrapod API server.

Uses lifespan handler for startup/shutdown with async resource management.
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from terrapod.auth.connectors import init_connectors
from terrapod.config import settings
from terrapod.db.session import close_db, get_db_session, init_db
from terrapod.logging_config import configure_logging, get_logger
from terrapod.redis.client import close_redis, init_redis
from terrapod.services.encryption_service import init_encryption
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

    init_encryption()
    logger.info("Encryption initialized")

    # Initialize Certificate Authority
    from terrapod.auth.ca import init_ca

    try:
        async with get_db_session() as db:
            await init_ca(db)
        logger.info("Certificate Authority initialized")
    except Exception as e:
        logger.warning("CA initialization skipped (migration may be pending)", error=str(e))

    # Start VCS poller if enabled
    import asyncio

    vcs_poller_task = None
    if settings.vcs.enabled:
        from terrapod.services.vcs_poller import run_poller

        vcs_poller_task = asyncio.create_task(run_poller())
        logger.info("VCS poller started")

    yield

    # Stop VCS poller
    if vcs_poller_task is not None:
        vcs_poller_task.cancel()
        try:
            await vcs_poller_task
        except asyncio.CancelledError:
            pass
        logger.info("VCS poller stopped")

    # Shutdown
    logger.info("Shutting down Terrapod API server")
    await close_storage()
    await close_redis()
    await close_db()


def create_application() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Terrapod API",
        description="Terrapod - Open-source Terraform Enterprise replacement",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

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

    # TFE V2 compatibility routes
    from terrapod.api.routers.tfe_v2 import router as tfe_v2_router

    app.include_router(tfe_v2_router)

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

    # Caching routes (module mirror, provider mirror, binary cache)
    from terrapod.api.routers.module_mirror import router as module_mirror_router

    app.include_router(module_mirror_router)

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

    return app


# Application instance
app = create_application()
