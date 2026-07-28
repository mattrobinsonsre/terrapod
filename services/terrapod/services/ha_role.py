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
