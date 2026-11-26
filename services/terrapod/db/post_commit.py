"""Defer a side effect until after the transaction that describes it has committed.

An SSE event published from inside a transaction is a lie the subscriber can
catch. `transition_run` used to write `run.status`, `flush()`, and publish — all
before any commit — so a client that refetched on receipt could read the
*previous* status, because the row it describes was not yet visible to another
session. Whether that race is lost depends entirely on who commits and when:

* an API request commits in `get_db` as soon as the handler returns, so the
  window is the remainder of the handler;
* the run reconciler commits **once, outside its per-run loop**, so the window is
  every other in-flight run's reconciliation.

That is why a configure's terminal transition was the one that showed it. It has
no `/configure-result` POST — the reconciler is its only driver — and nothing
publishes after it, so the stale read was permanent rather than corrected a
moment later by the next event (#2720).

**Why a session event rather than publishing from the callers.** There are over
three hundred `commit()` calls in this package. Draining a queue at each is not
a real option, and asking a caller to publish after its own commit is the
"fifteen creators must each remember" shape this codebase keeps replacing with
one producer. One `after_commit` listener covers every commit site, wherever it
is, and changes no caller's transaction semantics.

The stash mechanism is `replication.install_outbox_hooks`'s, which parks state on
`session.info` between `before_flush` and `after_flush` for the same reason: it
is the only place to put per-transaction state that a session event can reach.

**A factory, not a coroutine.** A coroutine object created and never awaited —
which is what a rolled-back transaction would leave behind — raises
`RuntimeWarning: coroutine was never awaited` and, worse, is easy to leak. The
callable is invoked at drain time, so a transaction that never commits costs
nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from sqlalchemy import event
from sqlalchemy.orm import Session

logger = structlog.get_logger(__name__)

#: Key under which pending factories ride on `session.info` until the commit.
_STASH = "_terrapod_post_commit"

#: Hard bound on one deferred side effect.
#:
#: Matches `run_artifacts._PUBLISH_TIMEOUT_SECONDS`, and for the same reason: a
#: blackholed Redis must not stall anything. Applied here rather than at each
#: call site so every deferred publish is bounded by construction — the gap the
#: run-status publish had, where `run_artifacts.py` had already fixed it next
#: door for its own publishers.
TIMEOUT_SECONDS = 2.0

#: Strong references to scheduled work.
#:
#: `asyncio.create_task` does not keep its task alive — a task whose only
#: reference is a local is collectable the moment the scheduling frame returns,
#: and the coroutine then never runs. Holding it here until it completes is the
#: documented idiom.
_in_flight: set[asyncio.Task[None]] = set()

#: Set once the listeners are attached, so a second install is a no-op.
_installed = False


def run_after_commit(
    session: Any,
    factory: Callable[[], Coroutine[Any, Any, None]],
    *,
    what: str,
) -> None:
    """Queue `factory` to be called once this session's transaction commits.

    `session` may be an `AsyncSession` or a sync `Session`: `info` proxies to the
    same dict either way, which is what lets the listener below — which only ever
    sees the sync session — find what an async caller stashed.

    `what` names the side effect for the log line if it fails. It is not used for
    anything else, so it can be as specific as the caller likes.
    """
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        # A mock session, which in this codebase means a services-tier unit test.
        # Nothing is subscribed there and the draining listener is not attached
        # either, so there is nothing to defer and nothing to lose. It is checked
        # rather than assumed because `AsyncMock().info.setdefault(...)` returns a
        # *coroutine*, so the obvious form raises
        # `AttributeError: 'coroutine' object has no attribute 'append'` from
        # inside whatever transition the test was really exercising.
        #
        # This can only ever be a mock: a real `AsyncSession.info` is a dict, and
        # `test_a_real_session_info_is_a_dict` pins that so this no-op cannot
        # quietly become the production path.
        return
    pending: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]]
    pending = info.setdefault(_STASH, [])
    pending.append((what, factory))


async def _run_batch(
    pending: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]],
) -> None:
    """Run one transaction's side effects, in the order they were queued."""
    for what, factory in pending:
        try:
            await asyncio.wait_for(factory(), timeout=TIMEOUT_SECONDS)
        except Exception as e:
            # Best-effort by contract: the transaction has already committed, so
            # failing here must not affect it, and one failure must not stop the
            # rest of the batch. This matches what the publishers did inline.
            logger.debug("post-commit side effect failed", what=what, error=str(e))


def install_hooks() -> None:
    """Attach the drain listeners to the global `Session` class. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True

    @event.listens_for(Session, "after_commit")
    def _drain(session: Session) -> None:
        pending = session.info.pop(_STASH, None)
        if not pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop: a synchronous commit, which in this codebase means a
            # migration or a test fixture rather than a request. Nothing is
            # subscribed in either case, so dropping the side effect is correct
            # and must not raise into the commit that just succeeded.
            return
        assert loop is not None  # narrowed by the guard above
        # ONE task for the whole batch, running the factories IN ORDER.
        #
        # A task each is delivered in whatever order the loop happens to run
        # them, so a transaction moving a run `pending -> queued -> planning`
        # could put those three events on the wire in any sequence. A consumer
        # that refetches is unaffected -- it reads the row, not the event -- but
        # one tracking transitions would see them scrambled, and nothing here
        # needs them parallel: each publish is a sub-millisecond Redis write, and
        # each is separately bounded so a blackholed one cannot hold up the rest
        # beyond its own timeout.
        #
        # Fire-and-forget is the contract: the transaction has committed, so a
        # side effect that cannot affect it must not be awaited by the commit
        # either. The scheduler (principle 11) is for work that must run once per
        # CLUSTER; this runs once per COMMIT, in the process that committed.
        #
        # The directive must sit on the line semgrep anchors to, and `ruff format`
        # wrapping a long line would move it clear and silently turn the
        # suppression off -- the trap at `storage/location_resolver.py:278-285`.
        # A short local keeps the call and its directive together.
        work = _run_batch(pending)
        task = asyncio.create_task(work)  # nosemgrep: terrapod-no-raw-background-tasks
        _in_flight.add(task)
        task.add_done_callback(_in_flight.discard)

    @event.listens_for(Session, "after_rollback")
    def _discard(session: Session) -> None:
        # A transition that did not commit must not announce itself.
        session.info.pop(_STASH, None)
