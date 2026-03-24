"""Distributed task scheduler for multi-replica API deployments.

Coordinates background task execution across multiple API replicas using Redis.
No leader election — any replica can execute any task, with Redis providing
distributed mutual exclusion.

Two scheduling patterns:

Periodic tasks:
    Registered with name + interval. Each replica's scheduler loop uses
    Redis SET NX EX as a distributed mutex — exactly one replica executes
    per interval. The lock auto-expires, so if a replica crashes, another
    picks up the next cycle.

Triggered tasks:
    Event-driven work items pushed to a Redis LIST queue. Any replica's
    consumer loop dequeues and executes. Deduplication via Redis SET NX
    prevents duplicate items in the queue.

Redis keys:
    tp:sched:{name}:claim       — NX mutex for periodic tasks (TTL = interval)
    tp:sched:{name}:running     — set while task is executing (TTL = 3x interval)
    tp:sched:{name}:last        — UNIX timestamp of last completed execution
    tp:sched:triggers           — LIST queue for triggered tasks
    tp:sched:trigger:{dedup}    — NX dedup key for triggered tasks (TTL = 5min)
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from terrapod.api.metrics import (
    SCHEDULER_TASK_DURATION,
    SCHEDULER_TASK_EXECUTIONS,
    SCHEDULER_TRIGGER_DEDUPLICATED,
    SCHEDULER_TRIGGER_ENQUEUED,
    SCHEDULER_TRIGGER_PROCESSED,
)
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client

logger = get_logger(__name__)

PREFIX = "tp:sched"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class PeriodicTaskDef:
    """Definition of a periodic background task."""

    name: str
    interval_seconds: int
    handler: Callable[[], Awaitable[None]]
    description: str = ""


@dataclass
class TriggerHandlerDef:
    """Definition of a triggered task handler."""

    name: str
    handler: Callable[[dict], Awaitable[None]]
    description: str = ""


_periodic_tasks: dict[str, PeriodicTaskDef] = {}
_trigger_handlers: dict[str, TriggerHandlerDef] = {}


def register_periodic_task(
    name: str,
    interval_seconds: int,
    handler: Callable[[], Awaitable[None]],
    description: str = "",
) -> None:
    """Register a periodic task to be executed once per interval globally."""
    _periodic_tasks[name] = PeriodicTaskDef(name, interval_seconds, handler, description)
    logger.info("Registered periodic task", task=name, interval=interval_seconds)


def register_trigger_handler(
    name: str,
    handler: Callable[[dict], Awaitable[None]],
    description: str = "",
) -> None:
    """Register a handler for triggered tasks of the given type."""
    _trigger_handlers[name] = TriggerHandlerDef(name, handler, description)
    logger.info("Registered trigger handler", handler=name)


# ---------------------------------------------------------------------------
# Periodic task coordination
# ---------------------------------------------------------------------------


async def try_claim_periodic(name: str, interval_seconds: int) -> bool:
    """Atomically claim execution of a periodic task for this interval.

    Uses SET NX EX: if the key doesn't exist, sets it with TTL = interval.
    Returns True if this replica claimed the slot. The key auto-expires
    after interval_seconds, allowing the next cycle to be claimed.

    Also checks a "running" key to prevent overlap when tasks exceed their
    interval. The running key has TTL = 3x interval as a crash safety net.
    """
    redis = get_redis_client()

    # If a previous execution is still running, don't start another
    running_key = f"{PREFIX}:{name}:running"
    if await redis.exists(running_key):
        return False

    # Try to claim this interval slot
    claim_key = f"{PREFIX}:{name}:claim"
    result = await redis.set(claim_key, str(time.time()), nx=True, ex=interval_seconds)
    if not result:
        return False

    # Mark as running with generous TTL (auto-clears if replica crashes)
    await redis.set(running_key, str(time.time()), ex=interval_seconds * 3)
    return True


async def mark_completed(name: str) -> None:
    """Record that a periodic task completed successfully."""
    redis = get_redis_client()
    await redis.delete(f"{PREFIX}:{name}:running")
    await redis.set(f"{PREFIX}:{name}:last", str(time.time()))


async def get_last_run(name: str) -> float | None:
    """Get the UNIX timestamp of the last completed execution."""
    redis = get_redis_client()
    val = await redis.get(f"{PREFIX}:{name}:last")
    return float(val) if val else None


# ---------------------------------------------------------------------------
# Triggered task queue
# ---------------------------------------------------------------------------


async def enqueue_trigger(
    trigger_type: str,
    payload: dict | None = None,
    dedup_key: str | None = None,
    dedup_ttl: int = 300,
) -> bool:
    """Enqueue a triggered task for any replica to pick up.

    Args:
        trigger_type: Handler name to dispatch to.
        payload: Arbitrary data passed to the handler.
        dedup_key: If set, prevents duplicate enqueues while a matching
            key exists. The dedup key auto-expires after dedup_ttl seconds.
        dedup_ttl: TTL for the dedup key (default 5 minutes).

    Returns True if enqueued, False if deduplicated.
    """
    redis = get_redis_client()

    if dedup_key:
        # Atomic dedup: SET NX with TTL. If already set, item is pending.
        dedup_redis_key = f"{PREFIX}:trigger:{dedup_key}"
        added = await redis.set(dedup_redis_key, "1", nx=True, ex=dedup_ttl)
        if not added:
            SCHEDULER_TRIGGER_DEDUPLICATED.labels(type=trigger_type).inc()
            logger.debug("Trigger deduplicated", type=trigger_type, dedup_key=dedup_key)
            return False

    item = json.dumps(
        {
            "type": trigger_type,
            "payload": payload or {},
            "dedup_key": dedup_key,
            "enqueued_at": time.time(),
        }
    )
    await redis.lpush(f"{PREFIX}:triggers", item)
    SCHEDULER_TRIGGER_ENQUEUED.labels(type=trigger_type).inc()
    logger.info("Trigger enqueued", type=trigger_type, dedup_key=dedup_key)
    return True


async def _clear_dedup(dedup_key: str | None) -> None:
    """Clear a dedup key after processing."""
    if dedup_key:
        redis = get_redis_client()
        await redis.delete(f"{PREFIX}:trigger:{dedup_key}")


# ---------------------------------------------------------------------------
# Scheduler loops
# ---------------------------------------------------------------------------


async def _run_periodic_loop(
    task: PeriodicTaskDef,
    shutdown: asyncio.Event,
) -> None:
    """Background loop for a single periodic task."""
    logger.info(
        "Periodic task loop started",
        task=task.name,
        interval=task.interval_seconds,
    )
    while not shutdown.is_set():
        try:
            if await try_claim_periodic(task.name, task.interval_seconds):
                logger.debug("Claimed periodic task", task=task.name)
                start = time.monotonic()
                try:
                    await task.handler()
                    SCHEDULER_TASK_EXECUTIONS.labels(task=task.name, status="success").inc()
                except Exception as e:
                    SCHEDULER_TASK_EXECUTIONS.labels(task=task.name, status="error").inc()
                    logger.error(
                        "Periodic task failed",
                        task=task.name,
                        error=str(e),
                        exc_info=e,
                    )
                finally:
                    SCHEDULER_TASK_DURATION.labels(task=task.name).observe(time.monotonic() - start)
                    await mark_completed(task.name)
        except Exception as e:
            logger.error("Scheduler claim error", task=task.name, error=str(e))

        # Wait for interval or shutdown
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=task.interval_seconds)
            break  # shutdown signaled
        except TimeoutError:
            pass  # interval elapsed, try again

    logger.info("Periodic task loop stopped", task=task.name)


async def _run_trigger_consumer(shutdown: asyncio.Event) -> None:
    """Background loop consuming triggered tasks from the Redis queue."""
    redis = get_redis_client()
    queue_key = f"{PREFIX}:triggers"
    logger.info("Trigger consumer started")

    while not shutdown.is_set():
        try:
            # BRPOP with short timeout so we can check shutdown regularly
            result = await redis.brpop(queue_key, timeout=2)
            if result is None:
                continue

            _, raw = result
            item = json.loads(raw)
            trigger_type = item["type"]
            payload = item.get("payload", {})
            dedup_key = item.get("dedup_key")

            handler_def = _trigger_handlers.get(trigger_type)
            if handler_def:
                logger.info("Executing trigger", type=trigger_type)
                try:
                    await handler_def.handler(payload)
                    SCHEDULER_TRIGGER_PROCESSED.labels(type=trigger_type, status="success").inc()
                except Exception as e:
                    SCHEDULER_TRIGGER_PROCESSED.labels(type=trigger_type, status="error").inc()
                    logger.error(
                        "Trigger handler failed",
                        type=trigger_type,
                        error=str(e),
                        exc_info=e,
                    )
                finally:
                    await _clear_dedup(dedup_key)
            else:
                logger.warning("No handler for trigger type", type=trigger_type)
                await _clear_dedup(dedup_key)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Trigger consumer error", error=str(e))
            # Brief backoff on unexpected errors
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=1)
                break
            except TimeoutError:
                pass

    logger.info("Trigger consumer stopped")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

_scheduler_tasks: list[asyncio.Task] = []
_shutdown_event: asyncio.Event | None = None


async def start_scheduler() -> None:
    """Start all registered scheduler loops.

    Called from the API lifespan. Each periodic task gets its own asyncio
    task. A single trigger consumer processes the shared trigger queue.
    """
    global _shutdown_event  # noqa: PLW0603
    _shutdown_event = asyncio.Event()

    for task_def in _periodic_tasks.values():
        t = asyncio.create_task(
            _run_periodic_loop(task_def, _shutdown_event),
            name=f"sched:{task_def.name}",
        )
        _scheduler_tasks.append(t)

    if _trigger_handlers:
        t = asyncio.create_task(
            _run_trigger_consumer(_shutdown_event),
            name="sched:trigger_consumer",
        )
        _scheduler_tasks.append(t)

    logger.info(
        "Scheduler started",
        periodic_tasks=list(_periodic_tasks.keys()),
        trigger_handlers=list(_trigger_handlers.keys()),
    )


async def stop_scheduler() -> None:
    """Stop all scheduler loops gracefully."""
    if _shutdown_event:
        _shutdown_event.set()

    if _scheduler_tasks:
        await asyncio.gather(*_scheduler_tasks, return_exceptions=True)
        _scheduler_tasks.clear()

    logger.info("Scheduler stopped")


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


async def get_scheduler_status() -> dict:
    """Get status of all registered tasks for admin observability."""
    redis = get_redis_client()
    status: dict = {
        "periodic_tasks": {},
        "trigger_queue_length": 0,
        "trigger_handlers": list(_trigger_handlers.keys()),
    }

    for name, task_def in _periodic_tasks.items():
        last = await redis.get(f"{PREFIX}:{name}:last")
        claim_ttl = await redis.ttl(f"{PREFIX}:{name}:claim")
        is_running = await redis.exists(f"{PREFIX}:{name}:running")
        status["periodic_tasks"][name] = {
            "interval_seconds": task_def.interval_seconds,
            "description": task_def.description,
            "last_completed_at": float(last) if last else None,
            "next_eligible_in_seconds": max(0, claim_ttl) if claim_ttl > 0 else 0,
            "is_running": bool(is_running),
        }

    queue_len = await redis.llen(f"{PREFIX}:triggers")
    status["trigger_queue_length"] = queue_len

    return status
