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
fire before any of them populated the cache. Callers share one in-flight task
rather than racing.

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
        self._tasks: dict[MetaKey, asyncio.Task] = {}
        self.hits = 0
        self.misses = 0

    async def get_or_fetch(self, key: MetaKey, fetch: Callable[[], Awaitable[T]]) -> T:
        """Return the cached result for ``key``, or run ``fetch`` to produce it.

        Concurrent callers for the same key await one shared task. The result —
        including an exception — is shared by all of them.
        """
        task = self._tasks.get(key)
        if task is None:
            self.misses += 1
            # create_task (not a bare coroutine) so concurrent callers can all
            # await the same handle; awaiting one coroutine twice is an error.
            task = asyncio.create_task(fetch())
            self._tasks[key] = task
        else:
            self.hits += 1
        return await task

    def stats(self) -> dict[str, int]:
        """Hit/miss counters, for logging how much upstream traffic was saved."""
        return {"hits": self.hits, "misses": self.misses}
