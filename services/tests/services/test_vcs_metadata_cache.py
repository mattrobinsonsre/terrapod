"""Tests for the per-poll-cycle VCS metadata cache (#1096)."""

import asyncio

import pytest

from terrapod.services.vcs_metadata_cache import VCSMetadataCache


class TestMemoisation:
    """The point of the cache: one upstream call per distinct key per cycle."""

    async def test_repeated_key_calls_upstream_once(self):
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            return "abc123"

        cache = VCSMetadataCache()
        key = ("conn-1", "org", "repo", "main")
        results = [await cache.get_or_fetch(key, fetch) for _ in range(5)]

        assert results == ["abc123"] * 5
        assert calls == 1, "five workspaces on one repo must not make five API calls"
        assert cache.stats() == {"hits": 4, "misses": 1}

    async def test_distinct_keys_each_call_upstream(self):
        calls = []

        def make(name):
            async def fetch():
                calls.append(name)
                return name

            return fetch

        cache = VCSMetadataCache()
        await cache.get_or_fetch(("c", "org", "repo-a", "main"), make("a"))
        await cache.get_or_fetch(("c", "org", "repo-b", "main"), make("b"))
        # Same repo, different branch — genuinely different question.
        await cache.get_or_fetch(("c", "org", "repo-a", "develop"), make("a-dev"))
        # Different connection, same repo — different credentials, so not shared.
        await cache.get_or_fetch(("c2", "org", "repo-a", "main"), make("a-conn2"))

        assert calls == ["a", "b", "a-dev", "a-conn2"]

    async def test_branch_sha_and_pr_lookups_do_not_collide(self):
        """Both lookups key on (conn, owner, repo, branch); they must stay distinct.

        The PR listing prefixes its branch slot, so a workspace asking for the
        head SHA of `main` must not receive the PR list for `main`.
        """
        cache = VCSMetadataCache()

        async def sha():
            return "sha-value"

        async def prs():
            return ["pr-value"]

        got_sha = await cache.get_or_fetch(("c", "org", "repo", "main"), sha)
        got_prs = await cache.get_or_fetch(("c", "org", "repo", "prs:main"), prs)

        assert got_sha == "sha-value"
        assert got_prs == ["pr-value"]


class TestSingleFlight:
    """Workspaces are polled concurrently, so caching alone is not enough."""

    async def test_concurrent_callers_share_one_call(self):
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()

        async def fetch():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()  # hold the call open while the others arrive
            return "shared"

        cache = VCSMetadataCache()
        key = ("c", "org", "repo", "main")
        tasks = [asyncio.create_task(cache.get_or_fetch(key, fetch)) for _ in range(10)]
        await started.wait()
        release.set()
        results = await asyncio.gather(*tasks)

        assert results == ["shared"] * 10
        assert calls == 1, "ten concurrent pollers must not each fire a request"


class TestFailureSharing:
    """A failing repo should cost one call, not one per workspace."""

    async def test_failure_is_shared_not_retried_per_caller(self):
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            raise RuntimeError("rate limited")

        cache = VCSMetadataCache()
        key = ("c", "org", "repo", "main")

        for _ in range(4):
            with pytest.raises(RuntimeError, match="rate limited"):
                await cache.get_or_fetch(key, fetch)

        # This is the behaviour that matters during a rate-limit event: four
        # workspaces on a failing repo spend one call, not four.
        assert calls == 1

    async def test_concurrent_callers_share_the_failure(self):
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            raise RuntimeError("boom")

        cache = VCSMetadataCache()
        key = ("c", "org", "repo", "main")
        results = await asyncio.gather(
            *[cache.get_or_fetch(key, fetch) for _ in range(5)],
            return_exceptions=True,
        )

        assert all(isinstance(r, RuntimeError) for r in results)
        assert calls == 1


class TestIsolation:
    """A fresh cache per cycle is what keeps polling correct."""

    async def test_a_new_cache_does_not_inherit_results(self):
        """Holding results across cycles would stop the poller seeing new commits."""
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            return f"sha-{calls}"

        key = ("c", "org", "repo", "main")
        first = VCSMetadataCache()
        assert await first.get_or_fetch(key, fetch) == "sha-1"

        second = VCSMetadataCache()
        assert await second.get_or_fetch(key, fetch) == "sha-2"
        assert calls == 2


class TestOneCallersCancellationStaysItsOwn:
    """Why the fetch runs in the caller's own coroutine rather than a shared task (#1156).

    A shared `asyncio.Task` makes cancellation contagious. `Task.cancel()`
    cancels whatever the task is awaiting, so cancelling *one* caller cancelled
    the task every other caller for that key was waiting on — and left a
    cancelled task in the cache, so every later caller inherited it too.

    In the poller that is not hypothetical: workspaces are polled concurrently
    under a semaphore, so one workspace's poll being cancelled (a timeout,
    shutdown, a task group tearing down) took every other workspace sharing that
    repository with it, and then every workspace on that repository for the rest
    of the cycle.
    """

    async def test_cancelling_one_caller_does_not_cancel_another(self):
        started = asyncio.Event()

        async def fetch():
            started.set()
            await asyncio.sleep(0.05)
            return "sha"

        cache = VCSMetadataCache()
        key = ("c", "o", "r", "main")
        a = asyncio.create_task(cache.get_or_fetch(key, fetch))
        await started.wait()
        b = asyncio.create_task(cache.get_or_fetch(key, fetch))
        await asyncio.sleep(0)  # let B reach its await
        a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await a

        assert await b == "sha", (
            "B was collaterally cancelled by A — it asked about a repository and "
            "had nothing to do with A"
        )

    async def test_a_cancelled_caller_does_not_poison_the_key(self):
        """A cancelled caller is not a failed repository. The next caller should
        get a real attempt rather than inherit somebody else's cancellation."""
        attempts = 0
        release = asyncio.Event()

        async def fetch():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                release.set()
                await asyncio.sleep(10)
            return f"sha-{attempts}"

        cache = VCSMetadataCache()
        key = ("c", "o", "r", "main")
        first = asyncio.create_task(cache.get_or_fetch(key, fetch))
        await release.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        assert await cache.get_or_fetch(key, fetch) == "sha-2"
        assert attempts == 2

    async def test_the_lock_is_released_when_a_caller_is_cancelled(self):
        """The corollary — a cancellation that left the per-key lock held would
        wedge every later caller for that repository for the rest of the cycle."""
        release = asyncio.Event()

        async def slow():
            release.set()
            await asyncio.sleep(10)
            return "never"

        async def quick():
            return "ok"

        cache = VCSMetadataCache()
        key = ("c", "o", "r", "main")
        first = asyncio.create_task(cache.get_or_fetch(key, slow))
        await release.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        assert await asyncio.wait_for(cache.get_or_fetch(key, quick), timeout=1) == "ok"

    def test_no_raw_task_is_created(self):
        """The rule this was flagged by (`terrapod-no-raw-background-tasks`)
        guards a real invariant, so satisfying it is pinned rather than left to
        the scanner."""
        import inspect

        source = inspect.getsource(VCSMetadataCache)

        assert "create_task" not in source
        assert "asyncio.Lock()" in source
