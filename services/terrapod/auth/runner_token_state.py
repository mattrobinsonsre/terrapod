"""Whether a runner token's run is still one a runner may act on.

A runner token is a stateless HMAC: verifying it proves the API minted it and
that it has not expired, and nothing more. So before GHSA-xmrf-hxq9-m59m a token
stayed usable for its whole TTL — up to two hours — regardless of what had
happened to the run, including after it reached a terminal state and throughout a
debug linger. `docs/runners.md` claimed the token was scoped to a run *and its
phase*; the phase half is `runner_tokens`, and this is the lifetime half.

Two mechanisms, and they need each other:

* **A revocation marker**, written when a run reaches a terminal state. This is
  the fast path and the only one that is prompt: it makes the token stop working
  at the moment the run ends rather than whenever a cache happens to lapse.
* **A terminal-state check** against the run row. This is the authority, and it
  is what covers a token whose marker was never written — a run that ended before
  this shipped, a replica that failed to publish, a flushed Redis.

The marker alone would be a promise nothing verifies; the row check alone would
cost a query on every runner request, and a runner makes hundreds per run
(provider downloads especially). So the answer is cached in Redis for a few
seconds, and the marker overwrites that cache rather than waiting for it, which
is why "cached active" does not delay a revocation in practice.

**Fail OPEN on an infrastructure error, closed on an answer.** If Redis cannot be
reached the row is consulted; if neither can be reached the request is allowed,
because refusing would stop every live run on a transient Redis blip — a far
worse outcome than a token remaining usable for a few more seconds. What is never
allowed is a run that is *known* to be terminal or gone.
"""

import uuid

from terrapod.logging_config import get_logger

logger = get_logger(__name__)

_STATE_PREFIX = "tp:runtok_state:"

#: Seconds an "active" answer is reused. Short, because it is the only window in
#: which a revocation could be missed — and the marker write overwrites the key
#: rather than waiting for it to lapse, so in practice the window is the gap
#: between the terminal transition and the marker, not this.
_ACTIVE_TTL = 15

_REVOKED = "revoked"
_ACTIVE = "active"

#: Terminal run statuses. Deliberately a copy rather than an import: this module
#: is in the auth path, and importing `run_service` from there pulls the whole
#: service layer (and its own imports back into auth) into every request's import
#: graph. `tests/auth/test_runner_token_state.py` asserts this equals
#: `run_service.TERMINAL_STATES`, so the copy cannot drift unnoticed.
TERMINAL_STATES: frozenset[str] = frozenset({"applied", "errored", "discarded", "canceled"})


def _key(run_id: str | uuid.UUID) -> str:
    return f"{_STATE_PREFIX}{run_id}"


def _marker_ttl() -> int:
    """How long a revocation marker outlives the run.

    The maximum life of any token for that run, so the marker cannot expire while
    a token it revokes is still signature-valid. Plus a margin, because a token
    minted a moment before the terminal transition expires a moment after the
    marker would otherwise.
    """
    from terrapod.config import load_runner_config

    try:
        configured = load_runner_config().max_token_ttl_seconds
    except Exception:  # noqa: BLE001 - config read must never break a transition
        configured = 0
    return max(int(configured or 0), 7200) + 300


async def revoke_run_tokens(run_id: str | uuid.UUID) -> None:
    """Mark every token for this run as no longer usable.

    Called on a terminal transition. Best-effort and silent: a run must reach its
    terminal state whether or not Redis is reachable, and the terminal-state check
    below is what makes the outcome correct anyway — this only makes it prompt.
    """
    try:
        from terrapod.redis.client import get_redis_client

        await get_redis_client().set(_key(run_id), _REVOKED, ex=_marker_ttl())
    except Exception:  # noqa: BLE001
        logger.debug("runner_token_revocation_marker_not_written", run_id=str(run_id))


async def is_run_token_usable(run_id: str, db=None) -> bool:  # type: ignore[no-untyped-def]
    """Whether a runner token for this run may still be used.

    False once the run is terminal (or gone). ``db`` is the session to consult the
    run row with; when it is None only the marker is read, which is the right
    trade for a caller that has no session to hand — it still honours a
    revocation and simply cannot self-heal a missing marker.
    """
    redis = None
    try:
        from terrapod.redis.client import get_redis_client

        redis = get_redis_client()
        cached = await redis.get(_key(run_id))
        if cached is not None:
            value = cached.decode() if isinstance(cached, bytes) else str(cached)
            if value == _REVOKED:
                return False
            if value == _ACTIVE:
                return True
    except Exception:  # noqa: BLE001
        # Unreachable or unconfigured. Fall through to the row, which is the
        # authority; never refuse on an infrastructure error.
        redis = None

    if db is None:
        return True

    try:
        status = await _run_status(db, run_id)
    except Exception:  # noqa: BLE001
        logger.warning("runner_token_run_status_unreadable", run_id=str(run_id), exc_info=True)
        return True

    if status is None or status in TERMINAL_STATES:
        # Self-healing: a run that ended without a marker gets one now, so the
        # next request on this token is answered from Redis.
        if redis is not None:
            try:
                await redis.set(_key(run_id), _REVOKED, ex=_marker_ttl())
            except Exception:  # noqa: BLE001
                pass
        return False

    if redis is not None:
        try:
            await redis.set(_key(run_id), _ACTIVE, ex=_ACTIVE_TTL)
        except Exception:  # noqa: BLE001
            pass
    return True


async def is_run_token_usable_on_its_own_session(run_id: str) -> bool:
    """`is_run_token_usable` for a caller that holds no session.

    The three surfaces with their own auth function — the SSE variant, the
    package-cache proxy and the OCI registry — each need a short-lived session for
    the row check and must not fall over when one cannot be had: a database that
    is briefly away should not turn a runner's pull into a 500 on a path that
    previously needed no I/O at all. So the session is best-effort and the marker
    is still honoured without it, which keeps a revocation effective even then.
    """
    from terrapod.db.session import get_db_session

    try:
        async with get_db_session() as db:
            return await is_run_token_usable(run_id, db)
    except Exception:  # noqa: BLE001
        logger.debug("runner_token_state_session_unavailable", run_id=str(run_id))
        return await is_run_token_usable(run_id, None)


async def _run_status(db, run_id: str) -> str | None:  # type: ignore[no-untyped-def]
    """The run's status, or None when there is no such run.

    One indexed primary-key read of a single column — not the whole Run row,
    which would load every artifact flag and option on every runner request.
    """
    from sqlalchemy import select

    from terrapod.db.models import Run

    try:
        run_uuid = uuid.UUID(str(run_id).removeprefix("run-"))
    except ValueError:
        return None

    result = await db.execute(select(Run.status).where(Run.id == run_uuid))
    return result.scalar_one_or_none()
