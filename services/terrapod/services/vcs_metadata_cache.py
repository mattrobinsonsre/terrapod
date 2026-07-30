"""Per-poll-cycle memoisation of VCS repo-metadata calls (#1096).

The poller asks the provider two questions per workspace — the tracked branch's
head SHA, and the open PRs targeting it — plus the repo's default branch for
workspaces that don't pin one. All three are pure functions of
``(connection, owner, repo, branch)``, so every workspace beyond the first on a
given repo was issuing an identical request every cycle.

That is enough to exhaust a GitHub App installation's hourly quota on a modest
estate: 65 workspaces across 16 distinct ``(repo, branch)`` pairs was ~7,800
calls/hour against a 5,000/hour limit, and GitHub reports primary-limit
exhaustion as a 403, which reads like a permission error.

The cache is deliberately **per cycle**. Holding results across cycles would
stop the poller noticing new commits — the whole point of polling. A fresh
instance is created for each poll cycle and discarded with it.

It also **single-flights**: workspaces are polled concurrently under a
semaphore, so without it the first N concurrent pollers for one repo would each
fire before any of them populated the cache. Concurrent callers for one key
queue on a per-key lock, so exactly one request goes out and the rest read the
result it left behind.

The fetch deliberately runs **inside the first caller's own coroutine** rather
than in a shared task, because a shared task makes cancellation contagious.
``Task.cancel()`` cancels whatever the task is awaiting, so cancelling *one*
caller cancelled the task every other caller for that key was waiting on — and
left a cancelled task in the cache, so every later caller inherited it too. With
workspaces polled concurrently under a semaphore, one workspace's poll being
cancelled took every other workspace on that repository with it, and then every
workspace on that repository for the rest of the cycle.

A lock keeps a cancellation the property of the caller it happened to: the
waiters simply acquire the lock next and try, and nothing is cached, so the key
is not poisoned.

Failures are shared too, which is intentional. When a repo's metadata call
fails, every workspace on that repo would have failed the same way; sharing the
failure turns N doomed calls into one. That matters most during a rate-limit
event, when extra calls are exactly what you cannot afford. The next cycle
retries with a fresh cache.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from terrapod.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# (connection_id, owner, repo, branch-or-"") — `branch` is "" for the
# default-branch lookup, which does not take one.
MetaKey = tuple[str, str, str, str]


class VCSMetadataCache:
    """Single-flight memoisation of provider metadata calls for one poll cycle."""

    def __init__(self) -> None:
        #: Settled outcomes. An `Exception` value is a shared failure, which is
        #: as much a result as a success — see the module docstring.
        self._outcomes: dict[MetaKey, object] = {}
        self._locks: dict[MetaKey, asyncio.Lock] = {}
        self.hits = 0
        self.misses = 0

    async def get_or_fetch(self, key: MetaKey, fetch: Callable[[], Awaitable[T]]) -> T:
        """Return the cached result for ``key``, or run ``fetch`` to produce it.

        Concurrent callers for the same key queue on a per-key lock, so exactly
        one request goes out. The result — including an exception — is shared by
        all of them.
        """
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock

        async with lock:
            if key in self._outcomes:
                self.hits += 1
                outcome = self._outcomes[key]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome  # type: ignore[return-value]

            self.misses += 1
            try:
                result = await fetch()
            except Exception as exc:
                # Cached so the other workspaces on this repo do not each go and
                # fail the same way — which matters most during a rate-limit
                # event, when extra calls are precisely what there is no room
                # for. `CancelledError` is a BaseException and so is deliberately
                # NOT cached: a cancelled caller is not a failed repo, and the
                # next caller should try rather than inherit the cancellation.
                self._outcomes[key] = exc
                raise
            self._outcomes[key] = result
            return result

    def stats(self) -> dict[str, int]:
        """Hit/miss counters, for logging how much upstream traffic was saved."""
        return {"hits": self.hits, "misses": self.misses}
