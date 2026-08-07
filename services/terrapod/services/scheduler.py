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

    Items travel in **lanes**, each a separate list with its own pool of
    consumers. Triggered tasks are mutually independent, so the only reason to
    process them one at a time is that a consumer awaits its handler inline —
    and for a long time there was exactly one consumer per replica for every
    kind of work at once. That made throughput (replicas x 1) regardless of how
    much was queued, so a poll cycle fanning out across an estate turned a set
    of independent, I/O-bound model calls into a multi-minute FIFO (#1296).
    Lanes let the AI class run several-at-a-time without also multiplying
    concurrency for the sub-second majority, and stop either class queueing
    behind the other.

Redis keys:
    tp:sched:{name}:claim       — NX mutex for periodic tasks (TTL = interval)
    tp:sched:{name}:running     — set while task is executing (TTL = 3x interval)
    tp:sched:{name}:last        — UNIX timestamp of last completed execution
    tp:sched:triggers           — LIST queue, `default` lane (name unchanged so
                                  a mid-upgrade replica keeps draining it)
    tp:sched:triggers:{lane}    — LIST queue for any non-default lane
    tp:sched:trigger:{dedup}    — NX dedup key for triggered tasks (TTL = 5min)
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from terrapod.api.metrics import (
    SCHEDULER_QUEUE_DEPTH,
    SCHEDULER_TASK_DURATION,
    SCHEDULER_TASK_EXECUTIONS,
    SCHEDULER_TRIGGER_DEDUPLICATED,
    SCHEDULER_TRIGGER_ENQUEUED,
    SCHEDULER_TRIGGER_PROCESSED,
    SCHEDULER_TRIGGER_WAIT,
)
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client
from terrapod.services import ha_role

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


#: The lane every handler uses unless it declares otherwise. Its queue key is
#: the historical `{PREFIX}:triggers`, unchanged — which is what makes lanes a
#: rolling-upgrade-safe addition: an un-upgraded replica keeps draining exactly
#: the key it always did, and only handlers that opt into a new lane move.
DEFAULT_LANE = "default"

#: Lane for work that is individually slow relative to the sub-second majority
#: (model calls). Not "slow" in absolute terms — the point is the *contrast*:
#: mixed into one FIFO with high-frequency status posts, a burst of these adds
#: minutes of head-of-line delay in both directions (#1296).
AI_LANE = "ai"

#: Per-replica AI-lane consumers when the operator has not said otherwise.
#: These handlers spend nearly all their time awaiting a model call, so this is
#: a concurrency ceiling rather than a thread count — the cost of a waiting
#: coroutine is negligible. It is a ceiling at all because each in-flight
#: handler holds a DB session and consumes provider rate-limit budget, and
#: because an unbounded fan-out would turn one busy poll cycle into a
#: thundering herd against the model endpoint.
DEFAULT_AI_LANE_CONSUMERS = 10


@dataclass
class TriggerHandlerDef:
    """Definition of a triggered task handler."""

    name: str
    handler: Callable[[dict], Awaitable[None]]
    description: str = ""
    #: Which queue lane this handler's items are enqueued to and consumed from.
    #: Lanes are isolated: each has its own Redis list and its own pool of
    #: consumers, so a saturated lane delays only itself.
    lane: str = DEFAULT_LANE


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
    lane: str = DEFAULT_LANE,
) -> None:
    """Register a handler for triggered tasks of the given type.

    `lane` picks which queue the type's items travel through. Leave it at the
    default unless the handler is materially slower than the sub-second norm:
    a lane exists to stop one class of work from delaying another, so putting
    everything in its own lane would just recreate the original problem with
    more moving parts.
    """
    _trigger_handlers[name] = TriggerHandlerDef(name, handler, description, lane)
    logger.info("Registered trigger handler", handler=name, lane=lane)


def _lane_queue_key(lane: str) -> str:
    """Redis list backing a lane.

    The default lane deliberately keeps the original un-suffixed key so that
    during a rolling upgrade an old replica and a new one are still reading and
    writing the same list. Only opted-in lanes get a new key, and those are
    written and read exclusively by upgraded replicas.
    """
    if lane == DEFAULT_LANE:
        return f"{PREFIX}:triggers"
    return f"{PREFIX}:triggers:{lane}"


def _consumers_for_lane(lane: str) -> int:
    """How many consumers this replica runs for a lane.

    The default lane stays at 1 so nothing about existing behaviour changes
    just because lanes arrived; the `ai` lane defaults higher because its items
    are independent and I/O-bound, which is the whole argument for concurrency.
    Operators can override per lane via `scheduler.lane_consumers`.

    Clamped to at least 1: a lane with a registered handler and zero consumers
    would accept enqueues and never drain them, which is a worse failure than
    any value an operator was trying to express by setting 0.
    """
    from terrapod.config import settings

    configured = (settings.scheduler.lane_consumers or {}).get(lane)
    if configured is None:
        configured = DEFAULT_AI_LANE_CONSUMERS if lane == AI_LANE else 1
    return max(1, int(configured))


def _lane_for(trigger_type: str) -> str:
    """Lane a trigger type belongs to, defaulting when the type is unknown.

    An unknown type reaching here means the enqueuing replica knows about a
    handler this one does not (mid-upgrade, or a follower running older code).
    Falling back to the default lane keeps such an item on the key every
    replica drains, so it is delayed rather than stranded.
    """
    handler_def = _trigger_handlers.get(trigger_type)
    return handler_def.lane if handler_def else DEFAULT_LANE


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
    lane = _lane_for(trigger_type)
    await redis.lpush(_lane_queue_key(lane), item)
    SCHEDULER_TRIGGER_ENQUEUED.labels(type=trigger_type).inc()
    logger.info("Trigger enqueued", type=trigger_type, dedup_key=dedup_key, lane=lane)
    return True


async def _clear_dedup(dedup_key: str | None) -> None:
    """Clear a dedup key after processing."""
    if dedup_key:
        redis = get_redis_client()
        await redis.delete(f"{PREFIX}:trigger:{dedup_key}")


# ---------------------------------------------------------------------------
# Scheduler loops
# ---------------------------------------------------------------------------


# Periodic tasks that must keep running on a FOLLOWER (#960).
#
# `ha_probe` is how a follower discovers it has become the leader — gating it
# would make the role permanently sticky, which is the one thing that must
# never happen.
#
# `encryption_key_refresh` propagates rotated DEKs into the in-process cache. A
# follower that stops running it cannot decrypt anything written after a
# rotation, and finds out at promotion — during the incident.
#
# `replication_sync` is the pull loop, and gating it would be self-defeating:
# converging with the leader is the entire job of a follower, and a follower
# that stops replicating is one that promotes with stale settings.
#
# `replication_purge` trims this node's own outbox. A follower still records
# events (it tags them with their origin so the pair cannot echo), so exempting
# the purge is what stops the follower's outbox growing without bound.
#
# `blob_sync` is the object-store half of the same pull loop (#1159), and the
# same argument applies with more force: a follower that stops copying state
# promotes with rows whose objects are not there — the failure that looks like
# success. It writes only into its own object store, never the leader's.
#
# All five are self-maintenance: none creates runs, mutates infrastructure, or
# originates a change an operator would see.
_FOLLOWER_SAFE_TASKS = frozenset(
    {
        "ha_probe",
        "encryption_key_refresh",
        "replication_sync",
        "replication_purge",
        "blob_sync",
    }
)

#: Tasks that must run ONLY on a follower — the pull side of replication.
#:
#: `_FOLLOWER_SAFE_TASKS` says "allowed on a follower"; it does not say "not on a
#: leader", so without this the leader pulled from its peer too. Found on a live
#: pair (#960): both nodes replicated from each other, each minting a token
#: against the other every cycle, which then tripped the peer's rate limit.
#:
#: It is also simply wrong by design. The follower pulls — that is what makes a
#: peer outage unable to block a healthy leader. A leader that pulls has given
#: itself a dependency on the node it is supposed to be able to survive.
_FOLLOWER_ONLY_TASKS = frozenset({"replication_sync", "blob_sync"})


def should_run_here(task_name: str, is_leader: bool) -> bool:
    """Whether this node should run `task_name`, given its role.

    Extracted from the loop so the three-way decision can be asserted directly
    rather than inferred by driving a `while` loop with a shutdown event. There
    are three cases and only the middle one is obvious:

    - **follower-only** (the pull side of replication) — the follower runs it,
      the leader does not.
    - **follower-safe** — either role may run it.
    - **everything else** — the leader only.
    """
    if task_name in _FOLLOWER_ONLY_TASKS:
        return not is_leader
    if task_name in _FOLLOWER_SAFE_TASKS:
        return True
    return is_leader


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
            # A follower runs no scheduled work. The write gate would refuse
            # the outcome anyway, but letting the task run and fail at the last
            # step is not equivalent to not running it: `vcs_poll` would burn
            # the installation's VCS API quota on every cycle, advance its own
            # poll cursor, and record a spurious poll failure on every VCS
            # workspace. Skipping is the correct behaviour, not an optimisation.
            if not should_run_here(task.name, await ha_role.is_leader()):
                logger.debug("Skipping periodic task: wrong role for this task", task=task.name)
            elif await try_claim_periodic(task.name, task.interval_seconds):
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


async def _run_trigger_consumer(shutdown: asyncio.Event, lane: str = DEFAULT_LANE) -> None:
    """Background loop consuming triggered tasks from one queue lane.

    This awaits its handler to completion before popping again, so one of these
    processes strictly one item at a time. That is not a property the work
    needs — triggered tasks are mutually independent, and the expensive ones are
    almost entirely `await`-ing a network call — it is simply what an inline
    `await handler(...)` in a pop loop gives you. Throughput is therefore
    (replicas x consumers-for-this-lane), and for a long time that was
    (replicas x 1) for everything, which is how a burst of independent AI
    summaries turned into a multi-minute FIFO (#1296).

    Concurrency comes from running several of these per lane
    (`scheduler.lane_consumers`); the lane split is what lets that number be
    raised for model calls without also raising it for the sub-second majority,
    and stops either class from queueing behind the other.

    Bounded rather than unbounded on purpose: in-flight handlers each hold a DB
    session and count against the model provider's rate limits and the daily
    token budget, so the ceiling is a capacity decision, not a formality.
    """
    redis = get_redis_client()
    queue_key = _lane_queue_key(lane)
    logger.info("Trigger consumer started", lane=lane)

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

            # How long this item sat in the queue, as distinct from how long
            # its handler then takes. Only the former is fixed by adding
            # consumers, so keeping them apart is what stops a capacity problem
            # being misread as a slow handler (#1296).
            enqueued_at = item.get("enqueued_at")
            if isinstance(enqueued_at, int | float):
                SCHEDULER_TRIGGER_WAIT.labels(type=trigger_type).observe(
                    max(0.0, time.time() - enqueued_at)
                )

            handler_def = _trigger_handlers.get(trigger_type)

            # The triggered-task consumer is a SECOND execution path, separate
            # from the periodic loops and fed directly by request handlers (a
            # VCS webhook, a policy sync, an onboarding step). A gate placed
            # around the periodic tasks does not cover it, so a follower would
            # otherwise create runs from a webhook that happened to land on it.
            #
            # The item is dropped rather than requeued: a follower has no
            # business doing this work, and the leader has its own queue.
            if handler_def and not await ha_role.is_leader():
                logger.info("Skipping trigger: not the leader", type=trigger_type)
                continue

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
            logger.error("Trigger consumer error", error=str(e), lane=lane)
            # Brief backoff on unexpected errors
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=1)
                break
            except TimeoutError:
                pass


async def _run_queue_depth_sampler(shutdown: asyncio.Event, interval: int = 15) -> None:
    """Publish each lane's backlog to `terrapod_scheduler_queue_depth`.

    Runs on every replica rather than leader-only. The depth is a property of
    one shared Redis list, so every replica reports the same number — which
    costs one cheap LLEN per lane per interval and means the signal survives a
    leadership change. Aggregate across pods with max() when graphing.

    Sampled on a timer rather than maintained inline because the interesting
    case is precisely when every consumer is busy: a counter incremented by the
    consumers themselves would stop updating exactly when the queue is deepest.
    """
    redis = get_redis_client()
    lanes = sorted({h.lane for h in _trigger_handlers.values()})

    while not shutdown.is_set():
        try:
            for lane in lanes:
                depth = await redis.llen(_lane_queue_key(lane))
                SCHEDULER_QUEUE_DEPTH.labels(lane=lane).set(depth)
        except asyncio.CancelledError:
            break
        except Exception as e:
            # Never let a metrics sample take the scheduler down with it.
            logger.warning("Queue depth sample failed", error=str(e))

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval)
            break
        except TimeoutError:
            continue

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

    lane_counts: dict[str, int] = {}
    if _trigger_handlers:
        _scheduler_tasks.append(
            asyncio.create_task(
                _run_queue_depth_sampler(_shutdown_event),
                name="sched:queue_depth_sampler",
            )
        )
        # One pool per lane that actually has a handler registered. Starting
        # consumers for a lane nobody uses would just be idle BRPOPs.
        for lane in sorted({h.lane for h in _trigger_handlers.values()}):
            count = _consumers_for_lane(lane)
            lane_counts[lane] = count
            for i in range(count):
                t = asyncio.create_task(
                    _run_trigger_consumer(_shutdown_event, lane),
                    name=f"sched:trigger_consumer:{lane}:{i}",
                )
                _scheduler_tasks.append(t)

    logger.info(
        "Scheduler started",
        periodic_tasks=list(_periodic_tasks.keys()),
        trigger_handlers=list(_trigger_handlers.keys()),
        lane_consumers=lane_counts,
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
