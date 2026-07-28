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
