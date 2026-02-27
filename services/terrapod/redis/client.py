"""
Redis client management for Terrapod API server.

Provides async Redis client, health checking, and FastAPI dependency injection.
Follows the same lifecycle pattern as db/session.py.
"""

from collections.abc import AsyncGenerator

import redis.asyncio as aioredis

from terrapod.config import settings
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Module-level client reference, initialized in lifespan
_redis: aioredis.Redis | None = None


async def init_redis() -> None:
    """Initialize Redis connection pool."""
    global _redis  # noqa: PLW0603
    logger.info("Initializing Redis connection")
    _redis = aioredis.from_url(
        str(settings.redis_url),
        decode_responses=True,
    )
    # Test connection
    await _redis.ping()
    logger.info("Redis connection established")


async def close_redis() -> None:
    """Close Redis connection pool."""
    global _redis  # noqa: PLW0603
    if _redis is not None:
        logger.info("Closing Redis connection pool")
        await _redis.aclose()
        _redis = None


def get_redis_client() -> aioredis.Redis:
    """Return the Redis client. Raises if not initialized."""
    if _redis is None:
        raise RuntimeError("Redis client not initialized — call init_redis() first")
    return _redis


async def get_redis() -> AsyncGenerator[aioredis.Redis]:
    """
    FastAPI dependency that provides a Redis client.

    Usage:
        @router.get("/example")
        async def example(redis: aioredis.Redis = Depends(get_redis)):
            await redis.get("key")
    """
    yield get_redis_client()


async def get_redis_health() -> bool:
    """Check Redis health for readiness probe."""
    try:
        if _redis is None:
            return False
        await _redis.ping()
        return True
    except Exception as e:
        logger.error("Redis health check failed", error=str(e))
        return False
