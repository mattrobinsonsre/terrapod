"""Leader/follower role resolution (#960 phase 1, #1101).

Terrapod never decides to fail over — a human does, by moving DNS. A node is
the leader if and only if it owns the shared name, and it discovers that by
probing the name and checking whether the answer is itself.

Three things this module is careful about:

**Role is node-level, not process-level.** The API runs several replicas behind
an HPA. If each held its own role in memory, a rolling upgrade would take the
node passive for a threshold's worth of time (every new pod starts unknown), a
scale-up would add a pod that disagrees with its siblings, and whether a write
was allowed would depend on which pod you landed on. So the resolved role lives
in the node's Redis, one replica probes, and every replica reads. A restart
inherits the current role rather than resetting it.

**A static role needs no Redis at all.** `role: leader` (the default, and what
the overwhelming majority of installs run) is answered from configuration
without touching Redis, so the common path gains no new dependency and no new
failure mode. Only `auto` consults Redis.

**Fail passive.** Under `auto`, a node that cannot determine its role does not
have one, and a node without a role is a follower.

NOTE for the enforcement phase: the probe must **not** be leadership-gated. A
follower has to keep probing or it can never discover that it has become the
leader. This is the same class of exception as the encryption key refresh.
"""

from __future__ import annotations

import time

import httpx

from terrapod.config import settings
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client

logger = get_logger(__name__)

LEADER = "leader"
FOLLOWER = "follower"

_PREFIX = "tp:ha"
_ROLE_KEY = f"{_PREFIX}:role"
_STREAK_KEY = f"{_PREFIX}:streak"
_PROBED_AT_KEY = f"{_PREFIX}:probed_at"
# The last role this node is known to have *held*, as opposed to the role it
# currently resolves to. Under `auto` those are the same key's job; under a
# static role there is no key at all, so a demotion by `helm upgrade --set
# api.config.ha.role=follower` would otherwise be invisible to the process that
# comes up afterwards (#1197).
_LAST_ROLE_KEY = f"{_PREFIX}:last_role"

# A probe is a single attempt with a short timeout, deliberately *not* wrapped
# in the shared retry helper. Retrying inside one probe would blur the
# observation it exists to make; tolerance comes from the threshold, which
# needs N *independent* observations to mean anything.
_PROBE_TIMEOUT_SECONDS = 10.0


def node_id() -> str:
    """This node's stable identity, as reported by the whoami endpoint."""
    return settings.ha.node_name


def is_auto() -> bool:
    return settings.ha.role == "auto"


async def get_role() -> str:
    """The role this node currently holds.

    Static configuration answers directly — no Redis, no I/O. Under `auto` the
    resolved role is read from the node's Redis, and an unreadable role means
    no role, which means follower.
    """
    if not is_auto():
        return settings.ha.role

    try:
        redis = get_redis_client()
        role = await redis.get(_ROLE_KEY)
    except Exception:
        logger.warning("HA role unreadable; failing passive to follower", exc_info=True)
        return FOLLOWER

    if role is None:
        # No role resolved yet — a node that has just started and not yet
        # probed is a follower until it earns leadership.
        return FOLLOWER
    return role.decode() if isinstance(role, bytes) else str(role)


async def is_leader() -> bool:
    return await get_role() == LEADER


async def _observe() -> str | None:
    """Probe the shared name once. Returns the node id that answered, or None."""
    url = settings.ha.effective_probe_url.rstrip("/")
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{url}/api/terrapod/v1/ha/whoami",
                headers={"Cache-Control": "no-store"},
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
            return str((data.get("attributes") or {}).get("node-id") or "")
    except Exception as e:
        logger.info("HA probe failed", url=url, error=repr(e))
        return None


async def probe_cycle() -> None:
    """One probe, and a role change once the streak reaches the threshold.

    Registered as a periodic task, so the scheduler's existing `SET NX EX`
    claim already guarantees exactly one replica probes per interval. The
    streak lives in Redis for the same reason the role does — the replica that
    probes may differ between intervals.

    The threshold applies symmetrically. Promotion and demotion take the same
    number of observations, not because that closes the cutover overlap (DNS
    cache governs that, not us) but because there is no reason for them to
    differ.
    """
    if not is_auto():
        return

    redis = get_redis_client()
    observed = await _observe()
    me = node_id()
    wanted = LEADER if (observed and observed == me) else FOLLOWER

    current = await get_role()
    if wanted == current:
        await redis.delete(_STREAK_KEY)
        await redis.set(_PROBED_AT_KEY, str(time.time()))
        return

    # Count consecutive observations pointing the other way.
    streak = await redis.get(_STREAK_KEY)
    prior = streak.decode() if isinstance(streak, bytes) else (streak or "")
    count = int(prior.split(":")[1]) + 1 if prior.startswith(f"{wanted}:") else 1

    if count >= settings.ha.probe_threshold:
        await redis.set(_ROLE_KEY, wanted)
        await redis.delete(_STREAK_KEY)
        await redis.set(_LAST_ROLE_KEY, wanted)
        await _retire_runs_on_role_change(previous=current, role=wanted)
        logger.warning(
            "HA role changed",
            node=me,
            previous=current,
            role=wanted,
            observed=observed or "(no answer)",
            after_observations=count,
        )
    else:
        await redis.set(_STREAK_KEY, f"{wanted}:{count}")
        logger.info(
            "HA probe disagrees with current role",
            node=me,
            role=current,
            observed=observed or "(no answer)",
            streak=f"{count}/{settings.ha.probe_threshold}",
        )

    await redis.set(_PROBED_AT_KEY, str(time.time()))


async def reconcile_role_on_startup() -> None:
    """Retire in-flight runs if this node booted into a different role (#1197).

    The probe handles a role change that happens *while the node is running*,
    and only under `auto`. A static role can only change across a restart —
    `helm upgrade --set api.config.ha.role=follower`, which is the demotion step
    the operations runbook actually tells people to perform — so without this
    the retirement predicate is never reached on the documented path.

    What that costs is a run left in `planning` for as long as the node stays a
    follower, holding its workspace at the per-workspace serialization gate. It
    cannot even time out there: `_check_stale` resolves through
    `transition_run`, which a follower refuses. A later promotion does
    eventually shed it — after another reconciler timeout — but "eventually,
    once you promote it again" is not a property to rely on, and a node parked
    as standby for a week holds those rows for a week.

    The trigger is strictly *the recorded role differs from the current one*,
    never *the process started*. A rolling restart on an unchanged role must
    retire nothing — the sibling replica is still driving those runs and their
    Jobs are still executing.

    No recorded role at all means this is a first boot, or a Redis that lost its
    data. Both record and do nothing: "cannot tell" must not become "kill the
    runs".

    Safe to run on every replica. Retirement is one idempotent bulk `UPDATE`, so
    whichever replica gets there first does the work and the rest find nothing.
    """
    role = await get_role()
    try:
        redis = get_redis_client()
        recorded = await redis.get(_LAST_ROLE_KEY)
        previous = recorded.decode() if isinstance(recorded, bytes) else (recorded or "")
        if previous and previous != role:
            logger.warning("Node booted into a different role", previous=previous, role=role)
            await _retire_runs_on_role_change(previous=previous, role=role)
        await redis.set(_LAST_ROLE_KEY, role)
    except Exception:
        # Never block startup on this. Failing to retire leaves the pre-#1197
        # behaviour, which is inert rather than harmful.
        logger.warning("Could not reconcile the node's role at startup", exc_info=True)


async def last_probe_age_seconds() -> float | None:
    """Seconds since the last probe completed, or None if it never has."""
    if not is_auto():
        return None
    try:
        raw = await get_redis_client().get(_PROBED_AT_KEY)
    except Exception:
        return None
    if raw is None:
        return None
    value = raw.decode() if isinstance(raw, bytes) else str(raw)
    return time.time() - float(value)


class NotLeaderError(RuntimeError):
    """Raised when a write is attempted on a node that is not the leader.

    Surfaced as HTTP 503: the request is well-formed and the caller is
    authorised, but this node is not currently the one serving writes. A client
    should retry against whoever holds the shared name.
    """

    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(
            f"this node is not the leader and cannot {action}; "
            "retry against the node holding the shared name"
        )


async def ensure_leader(action: str) -> None:
    """Refuse a write unless this node currently leads.

    Called at the *write*, not around the scheduler loops, because the
    scheduler is not the only thing that writes. The triggered-task consumer is
    a separate loop fed directly by request handlers, and the Slack socket is an
    outbound connection each replica dials — neither is reachable from a gate
    placed around the periodic tasks.

    Under the shipped default (`role: leader`) this always passes, so nothing in
    normal single-node operation ever reaches the raising branch. Tests are what
    exercise it; a false positive here takes down writes on a healthy node.
    """
    if not await is_leader():
        raise NotLeaderError(action)


async def _retire_runs_on_role_change(*, previous: str, role: str) -> None:
    """Mark this node's in-flight runs errored when its role changes.

    Not a quarantine subsystem — a predicate in the transition path, because
    that is all the situation needs.

    On **demotion** the runs in flight here are no longer ours to drive: this
    node will stop reconciling them, so leaving them `planning`/`applying`
    forever would strand their workspaces locked.

    On **promotion** the danger is sharper. A node that led before may hold run
    rows frozen at its last demotion, and several periodic tasks act on old rows
    without an upper age bound — `lifecycle_destroy_retry` in particular selects
    errored lifecycle destroys with no ceiling, so a promoted node could queue
    auto-applying destroys from a previous era. Retiring them first removes that
    input entirely.

    A direct UPDATE, deliberately: `transition_run` is leadership-gated (so it
    would refuse on demotion), and routing hundreds of rows through it would
    fire a notification and a commit status for each. This is bookkeeping, not
    a lifecycle event.
    """
    from sqlalchemy import update

    from terrapod.db.models import Run
    from terrapod.db.session import get_db_session

    non_terminal = ("pending", "queued", "planning", "planned", "confirmed", "applying")
    try:
        async with get_db_session() as db:
            result = await db.execute(
                update(Run)
                .where(Run.status.in_(non_terminal))
                .values(
                    status="errored",
                    error_message=(
                        f"Node role changed from {previous} to {role}; "
                        "this run was in flight and cannot be continued here"
                    ),
                )
            )
            await db.commit()
            if result.rowcount:
                logger.warning(
                    "Retired in-flight runs on role change",
                    previous=previous,
                    role=role,
                    runs=result.rowcount,
                )
    except Exception:
        logger.warning("Failed to retire in-flight runs on role change", exc_info=True)
