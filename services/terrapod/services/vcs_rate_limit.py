"""VCS provider rate-limit observation and API-call attribution (#1334).

Quota exhaustion on a VCS connection stops runs appearing across *every*
workspace on that connection, and it has caused several incidents in which the
budget state was invisible until someone read the logs. Two things fix that,
and they answer different questions:

* **How much is left** — recorded here from the rate-limit headers the provider
  already returns on every response, so an operator can see the budget draining
  before it hits zero.
* **Who is spending it** — a per-source request counter, because "the quota is
  gone" is only half an answer; the useful half is which subsystem burned it.

Nothing here makes an API call of its own. GitHub returns
``X-RateLimit-Limit``/``-Remaining``/``-Reset``/``-Used`` on every response
including the 403 you get once exhausted, and GitLab.com returns the
``RateLimit-*`` family, so the numbers ride along on requests the poller was
making anyway. That also means the observation is inherently *as of* the last
call rather than live, which is why the timestamp is recorded alongside it and
surfaced — a stale reading presented as current would be worse than none.

Everything is best-effort: a failure to record quota must never fail the VCS
operation that produced it.
"""

from __future__ import annotations

import contextlib
import functools
import time
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

from terrapod.api.metrics import (
    VCS_API_REQUESTS,
    VCS_RATE_LIMIT_LIMIT,
    VCS_RATE_LIMIT_REMAINING,
    VCS_RATE_LIMIT_RESET_SECONDS,
)
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Long enough to outlive any reset window (GitHub's is hourly), short enough
# that a deleted connection's last reading does not linger indefinitely.
_QUOTA_TTL_SECONDS = 6 * 60 * 60

# What spent the call. Set by whichever subsystem is driving, and read at the
# request chokepoint — the same provider function is reached from several
# subsystems, so the label has to come from the caller chain rather than the
# call site. `unknown` is a defect rather than a category: see
# tests/services/test_vcs_rate_limit.py, which asserts every entry point sets
# one.
SOURCE_UNKNOWN = "unknown"

_source: ContextVar[str] = ContextVar("vcs_call_source", default=SOURCE_UNKNOWN)


@contextlib.contextmanager
def vcs_source(name: str) -> Iterator[None]:
    """Attribute every provider call made in this block to `name`.

    Nests correctly and restores the previous value, so a subsystem that calls
    into another does not leave the label behind it.
    """
    token = _source.set(name)
    try:
        yield
    finally:
        _source.reset(token)


def current_source() -> str:
    return _source.get()


def attributed(name: str):  # type: ignore[no-untyped-def]
    """Decorator form, for a subsystem whose whole entry point is one source.

    Applied to the periodic cycle functions so attribution is declared once at
    the top of the subsystem rather than threaded through every provider call
    beneath it — the same function (`get_branch_sha`, say) is reached from
    several subsystems, so the label has to come from the caller chain.
    """

    def decorate(fn):  # type: ignore[no-untyped-def]
        @functools.wraps(fn)
        async def wrapper(*args: object, **kwargs: object) -> object:
            with vcs_source(name):
                return await fn(*args, **kwargs)

        return wrapper

    return decorate


@dataclass(frozen=True)
class RateLimitSnapshot:
    """What a provider last told us about the budget for one connection."""

    limit: int
    remaining: int
    reset_at: int  # epoch seconds
    observed_at: int  # epoch seconds — this is an observation, not a live read
    resource: str  # GitHub meters `core`, `search`, `graphql` separately

    @property
    def seconds_until_reset(self) -> int:
        return max(0, self.reset_at - int(time.time()))

    def to_attributes(self) -> dict[str, object]:
        """JSON:API attribute fragment (kebab-case, RFC3339 handled by caller)."""
        return {
            "rate-limit": self.limit,
            "rate-limit-remaining": self.remaining,
            "rate-limit-resource": self.resource,
        }


def _quota_key(connection_id: object) -> str:
    return f"tp:vcs_quota:{connection_id}"


def parse_headers(headers: object) -> RateLimitSnapshot | None:
    """Extract a snapshot from provider response headers, or None.

    Returns None when the server reports nothing — a self-hosted GitLab may
    have rate limiting switched off entirely, and an absent header must stay
    absent rather than being rendered as a full or empty budget.
    """
    get = getattr(headers, "get", None)
    if get is None:
        return None

    # GitHub uses X-RateLimit-*; GitLab uses the un-prefixed RateLimit-* family.
    def first(*names: str) -> str | None:
        for n in names:
            v = get(n)
            if v not in (None, ""):
                return str(v)
        return None

    raw_limit = first("X-RateLimit-Limit", "RateLimit-Limit")
    raw_remaining = first("X-RateLimit-Remaining", "RateLimit-Remaining")
    if raw_limit is None or raw_remaining is None:
        return None

    raw_reset = first("X-RateLimit-Reset", "RateLimit-Reset")
    resource = first("X-RateLimit-Resource") or "core"

    try:
        limit = int(raw_limit)
        remaining = int(raw_remaining)
        # GitHub's reset is an absolute epoch; GitLab's RateLimit-Reset is too
        # on .com, but a delta-seconds form exists in the draft spec. Treat a
        # small number as a delta rather than an epoch in 1970.
        reset = int(raw_reset) if raw_reset is not None else 0
    except ValueError:
        return None

    now = int(time.time())
    if 0 < reset < 10_000_000:
        reset = now + reset

    return RateLimitSnapshot(
        limit=limit,
        remaining=remaining,
        reset_at=reset,
        observed_at=now,
        resource=resource,
    )


async def record(conn: object, headers: object, *, outcome: str) -> None:
    """Record the budget and count the call. Never raises.

    Called from the provider request chokepoints on every response, so it sits
    directly in the path of every VCS operation — a failure here must cost
    observability and nothing else.
    """
    provider = str(getattr(conn, "provider", "") or "unknown")
    connection_id = getattr(conn, "id", None)
    source = current_source()

    try:
        VCS_API_REQUESTS.labels(provider=provider, source=source, outcome=outcome).inc()
    except Exception:  # pragma: no cover - a metrics registry failure
        pass

    snapshot = parse_headers(headers)
    if snapshot is None or connection_id is None:
        return

    name = str(getattr(conn, "name", "") or connection_id)
    try:
        VCS_RATE_LIMIT_LIMIT.labels(
            provider=provider, connection=name, resource=snapshot.resource
        ).set(snapshot.limit)
        VCS_RATE_LIMIT_REMAINING.labels(
            provider=provider, connection=name, resource=snapshot.resource
        ).set(snapshot.remaining)
        VCS_RATE_LIMIT_RESET_SECONDS.labels(
            provider=provider, connection=name, resource=snapshot.resource
        ).set(snapshot.seconds_until_reset)
    except Exception:  # pragma: no cover
        pass

    try:
        from terrapod.redis.client import get_redis_client

        await get_redis_client().hset(  # type: ignore[misc]
            _quota_key(connection_id),
            mapping={
                "limit": snapshot.limit,
                "remaining": snapshot.remaining,
                "reset_at": snapshot.reset_at,
                "observed_at": snapshot.observed_at,
                "resource": snapshot.resource,
            },
        )
        await get_redis_client().expire(_quota_key(connection_id), _QUOTA_TTL_SECONDS)
    except Exception as e:
        # Best effort, and deliberately not a traceback: a sustained Redis
        # outage would reach this line on every provider call.
        logger.debug(
            "Could not record VCS rate-limit observation",
            connection_id=str(connection_id),
            error=repr(e),
        )


async def get_snapshot(connection_id: object) -> RateLimitSnapshot | None:
    """Last recorded budget for a connection, or None if nothing is known."""
    try:
        from terrapod.redis.client import get_redis_client

        raw = await get_redis_client().hgetall(_quota_key(connection_id))  # type: ignore[misc]
    except Exception:
        return None
    if not raw:
        return None

    def field(key: str) -> str | None:
        v = raw.get(key) or raw.get(key.encode() if isinstance(key, str) else key)
        if isinstance(v, bytes):
            return v.decode()
        return None if v is None else str(v)

    try:
        return RateLimitSnapshot(
            limit=int(field("limit") or 0),
            remaining=int(field("remaining") or 0),
            reset_at=int(field("reset_at") or 0),
            observed_at=int(field("observed_at") or 0),
            resource=field("resource") or "core",
        )
    except ValueError:
        return None
