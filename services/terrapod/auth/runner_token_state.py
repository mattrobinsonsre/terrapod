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
worse outcome than a token remaining usable for a few more seconds. A run that is
*known* to be terminal or gone is refused -- with one deliberate exception, the
end-of-phase upload grace below, which admits the four routes the runner writes
at exit and nothing else, for `TERMINAL_UPLOAD_GRACE_SECONDS` after the run ended
and only when that end time was recorded.
"""

import time
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

#: Seconds after a run goes terminal during which the runner's own END-OF-PHASE
#: uploads are still accepted (#2564).
#:
#: The runner writes the combined phase log and the resource profile at EXIT,
#: after `LogCapture` has closed its handle -- which is necessarily after the
#: `apply-result` POST that moved the run to `applied`. So by the time those two
#: uploads happen the token has already been revoked, and every successful apply
#: lost its stored log: the run page showed the last live snapshot until that
#: expired and then showed nothing. A failed phase lost its log the same way,
#: and that is the log most worth having.
#:
#: Two minutes covers the upload's own retry ladder with room to spare (the
#: reported run retried at +2.1s and +6.2s) while leaving a token useless for
#: anything an operator would mind. It is NOT a general reprieve: see
#: `GRACE_PATH_SUFFIXES`, which is the half that makes this narrow.
TERMINAL_UPLOAD_GRACE_SECONDS = 120

#: The only routes the grace applies to: what the runner writes at exit.
#: Write-once artifacts for a run that has already finished, on a token still
#: scoped to that run and phase -- so the widening is "may finish uploading its
#: own log", not "may act".
#:
#: `tests/auth/test_terminal_upload_grace.py` derives this from the runner's own
#: exit path (`phases/log_capture.py`, `phases/resource_profile.py`) rather than
#: trusting the list, because a new exit-time upload added there would otherwise
#: start silently 401ing exactly as these two did.
GRACE_PATH_SUFFIXES: frozenset[str] = frozenset(
    {
        "/artifacts/plan-log",
        "/artifacts/apply-log",
        "/artifacts/configure-log",
        "/resource-profile",
    }
)

#: Terminal run statuses. Deliberately a copy rather than an import: this module
#: is in the auth path, and importing `run_service` from there pulls the whole
#: service layer (and its own imports back into auth) into every request's import
#: graph. `tests/auth/test_runner_token_state.py` asserts this equals
#: `run_service.TERMINAL_STATES`, so the copy cannot drift unnoticed.
TERMINAL_STATES: frozenset[str] = frozenset({"applied", "errored", "discarded", "canceled"})


def _grace_key(run_id: str) -> str:
    """Where the end-of-phase grace reads the run's end time (#2564).

    Separate from the revocation marker ON PURPOSE. Stamping the marker itself
    would be cheaper by one key and is not safe across a rolling upgrade: a
    replica that predates the stamp compares the marker for equality with
    `revoked`, does not recognise `revoked:<ts>`, and on the `db is None` path
    -- the SSE variant, the package-cache proxy, the OCI registry -- falls
    through to ACCEPTING the token. The marker therefore keeps the exact bytes
    every version reads, and the stamp lives where only a reader that knows to
    look will find one.
    """
    return f"tp:runtok_grace:{run_id}"


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

        # The marker carries WHEN, so the end-of-phase upload grace can be
        # computed from it without a second key or a row read. A marker written
        # by a replica that predates this is a bare `revoked`, which parses as
        # "no grace" -- fail closed, which is the right way round during a
        # rolling upgrade.
        redis = get_redis_client()
        await redis.set(_key(run_id), _REVOKED, ex=_marker_ttl())
        # Second, and best-effort within the same guard: the marker is what
        # refuses the token and must not be delayed or risked for a stamp whose
        # only job is to widen a 120-second window.
        await redis.set(_grace_key(run_id), str(int(time.time())), ex=_marker_ttl())
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
                # Deliberately BARE, where `revoke_run_tokens` stamps the time:
                # this write happens when a request arrives, not when the run
                # ended, so a timestamp here would say the run went terminal now
                # and hand an hour-old token a fresh upload grace (#2564). An
                # unstamped marker grants none, which is the honest answer to
                # "we do not know when this run ended".
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


def path_is_grace_eligible(path: str) -> bool:
    """True for a route the end-of-phase grace may apply to.

    Suffix-matched because the same routes are served on the canonical and the
    deprecated native prefixes, and our own runner images still construct the
    deprecated one.
    """
    return any(path.endswith(s) for s in GRACE_PATH_SUFFIXES)


async def terminal_upload_grace_active(run_id: str) -> bool:
    """Whether this run went terminal recently enough to finish uploading.

    Read from the revocation marker alone, deliberately: the row says a run is
    terminal but not WHEN it became so, and `updated_at` moves for reasons that
    have nothing to do with the transition. A marker that is missing, bare, or
    unparseable yields False, so every uncertainty refuses.
    """
    try:
        from terrapod.redis.client import get_redis_client

        cached = await get_redis_client().get(_grace_key(run_id))
    except Exception:  # noqa: BLE001
        return False
    if cached is None:
        # No stamp: either nothing recorded when this run ended, or it ended
        # before this shipped. Either way the end time is unknown, so there is
        # no window -- an unanswerable question is a refusal.
        return False
    try:
        at = int(cached.decode() if isinstance(cached, bytes) else str(cached))
    except (ValueError, AttributeError):
        return False
    return 0 <= (time.time() - at) <= TERMINAL_UPLOAD_GRACE_SECONDS


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
