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


@dataclass(frozen=True)
class CallContext:
    """Who is making the call, and on whose behalf.

    `source` is the subsystem (workspace-poll, autodiscovery, …) — useful for
    "which feature is spending". `repo`, `consumer` and `kind` are the identity
    that actually matters during an incident: the question is not which feature
    is busy, it is which repo or workspace to go and fix. One misconfigured
    workspace among hundreds is invisible in a per-subsystem count.

    `labels` carries the consumer's labels, which is what makes the second
    remedy actionable. An operator over budget has two moves — poll less often,
    or split the load across more connections — and splitting needs to be along
    a line that means something. Terrapod has no teams; labels are how an estate
    is divided, so a per-label rollup is what turns "this connection is at 140%"
    into "team=platform is two thirds of it, give them their own connection".
    """

    source: str = SOURCE_UNKNOWN
    repo: str = ""
    consumer: str = ""  # workspace name, module coordinate, or policy set
    kind: str = ""  # workspace | module | policy-set — blank falls back to source
    labels: tuple[tuple[str, str], ...] = ()  # pairs, not a dict: this is frozen


# Default None rather than a shared instance: CallContext is frozen, so sharing
# one would be safe, but the linter is right that a mutable default here is a
# trap waiting for the first person who unfreezes it.
_ctx: ContextVar[CallContext | None] = ContextVar("vcs_call_ctx", default=None)
_NO_CONTEXT = CallContext()


def _label_pairs(labels: object) -> tuple[tuple[str, str], ...]:
    """Normalise an entity's labels into hashable, bounded pairs.

    Bounded on purpose: labels are operator-supplied and go on to become Redis
    hash fields, so a pathological set must not turn one poll into thousands of
    writes. Truncating is the right failure — a rollup over the first handful of
    labels still points at the right team.
    """
    if not isinstance(labels, dict):
        return ()
    out = []
    for k, v in sorted(labels.items()):
        if not isinstance(k, str) or k == "":
            continue
        out.append((k[:60], str(v)[:60]))
        if len(out) >= 8:
            break
    return tuple(out)


@contextlib.contextmanager
def vcs_source(
    name: str,
    *,
    repo: str = "",
    consumer: str = "",
    kind: str = "",
    labels: object = None,
) -> Iterator[None]:
    """Attribute every provider call made in this block.

    Nests correctly and restores the previous value, so a subsystem that calls
    into another does not leave its label behind. An inner block that names only
    a repo keeps the enclosing source.
    """
    outer = _ctx.get() or _NO_CONTEXT
    pairs = _label_pairs(labels) if labels is not None else ()
    token = _ctx.set(
        CallContext(
            source=name or outer.source,
            repo=repo or outer.repo,
            consumer=consumer or outer.consumer,
            kind=kind or outer.kind,
            labels=pairs or outer.labels,
        )
    )
    try:
        yield
    finally:
        _ctx.reset(token)


@contextlib.contextmanager
def vcs_target(
    *, repo: str = "", consumer: str = "", kind: str = "", labels: object = None
) -> Iterator[None]:
    """Name what the enclosing subsystem is currently working on.

    Used inside a cycle, per repo/workspace/module, so the spend lands against
    the thing an operator can act on rather than only the subsystem.
    """
    with vcs_source("", repo=repo, consumer=consumer, kind=kind, labels=labels):
        yield


def current_source() -> str:
    return (_ctx.get() or _NO_CONTEXT).source


def current_context() -> CallContext:
    return _ctx.get() or _NO_CONTEXT


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
    ctx = current_context()

    try:
        VCS_API_REQUESTS.labels(provider=provider, source=ctx.source, outcome=outcome).inc()
    except Exception:  # pragma: no cover - a metrics registry failure
        pass

    if connection_id is not None:
        await _tally(connection_id, ctx)

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


# ---------------------------------------------------------------------------
# Consumption: the rate, and who is causing it (#1339)
# ---------------------------------------------------------------------------
#
# A budget level cannot answer "is this configuration straining the limit".
# The budget refills on a fixed window, so right after a reset it reads healthy
# even when consumption is hopeless — an instance burning 11,400 calls/hour
# against a 5,000/hour budget looks fine for part of every hour. The rate
# against the refill is the signal, so calls are counted into per-minute
# buckets and summed over a rolling window.
#
# The per-consumer tally is kept in Redis rather than on the Prometheus labels
# on purpose: repo and workspace are unbounded, and an estate with thousands of
# workspaces would wreck the metric. Prometheus keeps subsystem granularity;
# this bounded, expiring view is what the UI reads.

_RATE_WINDOW_MINUTES = 60
_BUCKET_TTL_SECONDS = (_RATE_WINDOW_MINUTES + 5) * 60
_CONSUMER_TTL_SECONDS = 2 * 60 * 60
_TOP_CONSUMERS = 10


def _minute_key(connection_id: object, minute: int) -> str:
    return f"tp:vcs_rate:{connection_id}:{minute}"


def _consumer_key(connection_id: object) -> str:
    return f"tp:vcs_consumers:{connection_id}"


def _label_key(connection_id: object) -> str:
    return f"tp:vcs_labels:{connection_id}"


# Field separator inside the consumer hash. A unit separator rather than a colon
# because both halves are operator-supplied — a workspace may legitimately be
# called "a:b", and splitting on the wrong character would file it under a kind
# that does not exist.
_FIELD_SEP = "\x1f"


async def _tally(connection_id: object, ctx: CallContext) -> None:
    """Count one call into the rate window, its consumer, and its labels.

    Runs on every provider response, so it is one pipelined round trip rather
    than six sequential ones. `transaction=False` is required, not incidental:
    the three keys have different prefixes and therefore hash to different slots
    on a cluster-mode Redis, where a MULTI across slots is refused outright.
    """
    try:
        from terrapod.redis.client import get_redis_client

        r = get_redis_client()
        minute = int(time.time()) // 60
        mk = _minute_key(connection_id, minute)
        ck = _consumer_key(connection_id)

        # Identify by the thing an operator can act on, falling back to the
        # subsystem when a call genuinely has no repo (a token refresh, say).
        name = ctx.repo or ctx.consumer or f"({ctx.source})"
        kind = ctx.kind or ctx.source or SOURCE_UNKNOWN

        pipe = r.pipeline(transaction=False)
        pipe.incr(mk)
        pipe.expire(mk, _BUCKET_TTL_SECONDS)
        pipe.hincrby(ck, f"{kind}{_FIELD_SEP}{name}", 1)
        pipe.expire(ck, _CONSUMER_TTL_SECONDS)
        if ctx.labels:
            lk = _label_key(connection_id)
            for k, v in ctx.labels:
                pipe.hincrby(lk, f"{k}={v}", 1)
            pipe.expire(lk, _CONSUMER_TTL_SECONDS)
        await pipe.execute()
    except Exception:
        # Same contract as the budget recording: observability must never break
        # the operation that produced it.
        pass


@dataclass(frozen=True)
class Consumption:
    """Rate, headroom and a verdict — the answer to "are we straining it"."""

    calls_per_hour: int
    window_minutes: int
    seconds_to_reset: int
    remaining: int | None
    limit: int | None
    verdict: str  # idle | comfortable | tight | will_exhaust | exhausted
    exhausts_in_seconds: int | None
    top_consumers: list[dict]
    label_totals: list[dict]

    def to_attributes(self) -> dict:
        return {
            "calls-per-hour": self.calls_per_hour,
            "rate-window-minutes": self.window_minutes,
            "seconds-to-reset": self.seconds_to_reset,
            "saturation": self.verdict,
            "exhausts-in-seconds": self.exhausts_in_seconds,
            "top-consumers": self.top_consumers,
            "label-totals": self.label_totals,
        }


def _verdict(rate: int, remaining: int | None, seconds_to_reset: int) -> tuple[str, int | None]:
    """Classify, and say when it runs out if it is going to.

    The comparison that matters is projected spend over the rest of the window
    against what is left — not the level, and not the rate in isolation. A high
    rate with a big budget and a near reset is fine; a modest rate against a
    nearly-empty budget is not.
    """
    if remaining is None:
        return ("idle" if rate == 0 else "comfortable"), None
    if remaining <= 0:
        return "exhausted", 0
    if rate <= 0:
        return "idle", None

    per_second = rate / 3600.0
    exhausts_in = int(remaining / per_second)
    projected = per_second * seconds_to_reset

    if projected >= remaining:
        return "will_exhaust", exhausts_in
    if projected >= remaining * 0.7:
        return "tight", exhausts_in
    return "comfortable", exhausts_in


def _decode(v: object) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def _rank(raw: object, parse) -> list[dict]:  # type: ignore[no-untyped-def]
    """Decode a count hash into a descending, truncated list of entries."""
    entries = []
    for k, v in (raw or {}).items():  # type: ignore[union-attr]
        try:
            count = int(_decode(v))
        except ValueError:
            continue
        entry = parse(_decode(k))
        if entry is None:
            continue
        entry["calls"] = count
        entries.append(entry)
    entries.sort(key=lambda e: e["calls"], reverse=True)
    return entries[:_TOP_CONSUMERS]


async def get_consumption(connection_id: object, snapshot: object | None) -> Consumption | None:
    """Rate + verdict + who is spending, or None when nothing is known."""
    try:
        from terrapod.redis.client import get_redis_client

        r = get_redis_client()
        now_minute = int(time.time()) // 60
        keys = [_minute_key(connection_id, now_minute - i) for i in range(_RATE_WINDOW_MINUTES)]
        pipe = r.pipeline(transaction=False)
        pipe.mget(keys)
        pipe.hgetall(_consumer_key(connection_id))
        pipe.hgetall(_label_key(connection_id))
        values, raw_consumers, raw_labels = await pipe.execute()
        calls = sum(int(v) for v in (values or []) if v not in (None, ""))
    except Exception:
        return None

    def _consumer(field: str) -> dict | None:
        # kind\x1fname; a field without the separator predates the split and is
        # still worth showing, just without a kind.
        kind, sep, name = field.partition(_FIELD_SEP)
        return {"name": name, "kind": kind} if sep else {"name": field, "kind": ""}

    def _label(field: str) -> dict | None:
        key, sep, value = field.partition("=")
        return {"label": field, "key": key, "value": value} if sep else None

    remaining = getattr(snapshot, "remaining", None)
    limit = getattr(snapshot, "limit", None)
    secs = getattr(snapshot, "seconds_until_reset", 0) if snapshot is not None else 0
    verdict, eta = _verdict(calls, remaining, secs)

    return Consumption(
        calls_per_hour=calls,
        window_minutes=_RATE_WINDOW_MINUTES,
        seconds_to_reset=secs,
        remaining=remaining,
        limit=limit,
        verdict=verdict,
        exhausts_in_seconds=eta,
        top_consumers=_rank(raw_consumers, _consumer),
        label_totals=_rank(raw_labels, _label),
    )
