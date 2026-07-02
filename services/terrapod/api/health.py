"""
Health check endpoints for Terrapod API server.

Provides /health (liveness) and /ready (readiness) endpoints.
"""

from fastapi import APIRouter, Response, status

from terrapod.db.schema_version import schema_is_current
from terrapod.db.session import get_db_health
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_health
from terrapod.storage import get_storage_or_none

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


@router.get("/health", status_code=status.HTTP_200_OK)
async def health() -> dict[str, str]:
    """Liveness probe endpoint.

    Returns 200 if the API server is running.
    """
    return {"status": "healthy"}


@router.get("/ready", status_code=status.HTTP_200_OK)
async def ready(response: Response) -> dict[str, str | dict[str, str]]:
    """Readiness probe endpoint.

    Checks that critical subsystems are initialized.
    """
    checks: dict[str, str] = {}

    checks["database"] = "healthy" if await get_db_health() else "unhealthy"
    checks["redis"] = "healthy" if await get_redis_health() else "unhealthy"

    storage = get_storage_or_none()
    checks["storage"] = "healthy" if storage is not None else "unhealthy"

    # App ↔ schema skew guard (#544): a pod whose code expects a migration the
    # DB hasn't got must report NOT READY (and get pulled from the LB) rather
    # than 500 every request against the missing column. Only meaningful once
    # the DB is reachable — a DB-down failure already fails readiness above.
    if checks["database"] == "healthy":
        schema_ok, schema_detail = await schema_is_current()
        checks["migrations"] = "healthy" if schema_ok else f"behind: {schema_detail}"

    all_healthy = all(v == "healthy" for v in checks.values())

    if not all_healthy:
        logger.warning("Readiness check failed", checks=checks)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not ready", "checks": checks}

    return {"status": "ready", "checks": checks}
