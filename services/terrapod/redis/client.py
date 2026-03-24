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
        from terrapod.api.metrics import REDIS_ERRORS

        REDIS_ERRORS.labels(operation="health_check").inc()
        logger.error("Redis health check failed", error=str(e))
        return False


# ── Pub/Sub Helpers ───────────────────────────────────────────────────────

RUN_EVENTS_PREFIX = "tp:run_events:"
ADMIN_EVENTS_CHANNEL = "tp:admin_events"
WORKSPACE_LIST_EVENTS_CHANNEL = "tp:workspace_list_events"
LISTENER_EVENTS_PREFIX = "tp:listener_events:"  # per-pool channel
POOL_EVENTS_PREFIX = "tp:pool_events:"  # per-pool admin channel
JOB_STATUS_PREFIX = "tp:job_status:"  # per-run job status cache
LOG_STREAM_PREFIX = "tp:log_stream:"  # per-run live log cache


async def publish_event(channel: str, data: str) -> None:
    """Publish a message to a Redis pub/sub channel."""
    client = get_redis_client()
    await client.publish(channel, data)


async def subscribe_channel(channel: str) -> aioredis.client.PubSub:
    """Create a pub/sub subscription and return the PubSub object."""
    client = get_redis_client()
    pubsub = client.pubsub()
    await pubsub.subscribe(channel)
    return pubsub


async def publish_workspace_event(
    workspace_id: str, event_type: str, extra: dict | None = None
) -> None:
    """Publish a workspace-scoped event to both the per-workspace and workspace-list SSE channels.

    Silently catches errors — SSE notifications are best-effort and must never
    break the originating API request.
    """
    try:
        import json

        payload = {"event": event_type, "workspace_id": str(workspace_id), **(extra or {})}
        data = json.dumps(payload)
        await publish_event(f"{RUN_EVENTS_PREFIX}{workspace_id}", data)
        await publish_event(WORKSPACE_LIST_EVENTS_CHANNEL, data)
    except Exception:
        pass


async def publish_listener_event(pool_id: str, event: dict) -> None:
    """Publish an event to a pool's listener SSE channel."""
    import json

    channel = f"{LISTENER_EVENTS_PREFIX}{pool_id}"
    await publish_event(channel, json.dumps(event))


async def set_job_status(run_id: str, phase: str, status: str) -> None:
    """Store a Job status report in Redis for the reconciler.

    Keyed by {run_id}:{phase} to prevent plan-phase status from leaking
    into the apply phase (race condition where a late plan "succeeded"
    response would cause a premature "applied" transition).
    """
    import json
    import time

    client = get_redis_client()
    data = json.dumps({"status": status, "reported_at": time.time()})
    await client.setex(f"{JOB_STATUS_PREFIX}{run_id}:{phase}", 120, data)


async def get_job_status_from_redis(run_id: str, phase: str) -> str | None:
    """Get the most recent Job status report for a run phase."""
    import json

    client = get_redis_client()
    data = await client.get(f"{JOB_STATUS_PREFIX}{run_id}:{phase}")
    if data is None:
        return None
    return json.loads(data).get("status")


async def delete_job_status(run_id: str, phase: str) -> None:
    """Delete the cached Job status for a run phase."""
    client = get_redis_client()
    await client.delete(f"{JOB_STATUS_PREFIX}{run_id}:{phase}")
