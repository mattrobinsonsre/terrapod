"""The package cache against real Postgres and real storage (#1417).

The mocked tests cover shape. Everything here is about behaviour that only
exists once there is a database and an object store: that a miss actually
populates both, that a hit does not go back upstream, that access moves the
retention clock, and that two replicas racing a cold package do not produce a
500 in someone's `pip install`.

Upstream is a local fake rather than the real PyPI — a test that reaches the
internet is a test that fails when the internet does, and none of the behaviour
under test is upstream's.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest
from sqlalchemy import func, select

from terrapod.db.models import CachedPackageFile
from terrapod.db.session import get_db_session
from terrapod.services.package_cache.substrate import (
    Artifact,
    NotFoundUpstream,
    SealedError,
    UpstreamError,
    fetch_and_cache,
    get_or_fetch,
    lookup,
)
from terrapod.storage import get_storage

pytestmark = pytest.mark.asyncio

WHEEL = b"not really a wheel, but bytes are bytes" * 100
WHEEL_SHA = hashlib.sha256(WHEEL).hexdigest()


def _fake_upstream(*, calls: list[str], status: int = 200, body: bytes = WHEEL):
    """An httpx client whose transport answers locally and counts requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _artifact(filename: str = "flask-3.0.0-py3-none-any.whl") -> Artifact:
    return Artifact(
        ecosystem="pypi",
        name="flask",
        version="3.0.0",
        filename=filename,
        upstream_url=f"https://upstream.test/{filename}",
        digest=f"sha256:{WHEEL_SHA}",
    )


class TestFetchAndCache:
    async def test_a_miss_populates_the_row_and_the_object(self, app) -> None:
        calls: list[str] = []
        storage = get_storage()

        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            record = await fetch_and_cache(db, storage, _artifact(), client=client)

        assert len(calls) == 1
        assert record.size == len(WHEEL)
        # The digest is upstream's, recorded as published rather than recomputed.
        assert record.digest == f"sha256:{WHEEL_SHA}"
        assert await storage.get(record.storage_key) == WHEEL

    async def test_a_hit_does_not_go_upstream_again(self, app) -> None:
        """The entire point of a cache, and the easiest thing to get wrong."""
        calls: list[str] = []
        storage = get_storage()

        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            await get_or_fetch(db, storage, _artifact(), client=client)
        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            await get_or_fetch(db, storage, _artifact(), client=client)

        assert len(calls) == 1

    async def test_reading_moves_the_retention_clock(self, app) -> None:
        """Retention sweeps on last access.

        An artifact every run installs would otherwise be evicted for being old
        and re-fetched immediately — a loop that costs bandwidth forever.
        """
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import update

        storage = get_storage()
        calls: list[str] = []
        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            record = await fetch_and_cache(db, storage, _artifact(), client=client)
            record_id = record.id

        stale = datetime.now(UTC) - timedelta(days=60)
        async with get_db_session() as db:
            await db.execute(
                update(CachedPackageFile)
                .where(CachedPackageFile.id == record_id)
                .values(last_accessed_at=stale)
            )

        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            await get_or_fetch(db, storage, _artifact(), client=client)

        async with get_db_session() as db:
            refreshed = await db.get(CachedPackageFile, record_id)
            assert refreshed.last_accessed_at > stale

    async def test_a_row_whose_object_vanished_is_refetched(self, app) -> None:
        """An operator who pruned the bucket should get a working install.

        Serving the stale row would be a redirect to a 404 that nobody can trace
        back to a database row.
        """
        calls: list[str] = []
        storage = get_storage()

        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            record = await fetch_and_cache(db, storage, _artifact(), client=client)
            key = record.storage_key

        await storage.delete(key)

        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            recovered = await get_or_fetch(db, storage, _artifact(), client=client)

        assert len(calls) == 2
        assert await storage.get(recovered.storage_key) == WHEEL
        async with get_db_session() as db:
            count = await db.scalar(select(func.count()).select_from(CachedPackageFile))
        assert count == 1

    async def test_a_404_upstream_is_distinguishable_from_a_failure(self, app) -> None:
        calls: list[str] = []
        storage = get_storage()
        async with get_db_session() as db, _fake_upstream(calls=calls, status=404) as client:
            with pytest.raises(NotFoundUpstream):
                await fetch_and_cache(db, storage, _artifact(), client=client)

    async def test_a_500_upstream_is_an_upstream_error(self, app) -> None:
        calls: list[str] = []
        storage = get_storage()
        async with get_db_session() as db, _fake_upstream(calls=calls, status=503) as client:
            with pytest.raises(UpstreamError):
                await fetch_and_cache(db, storage, _artifact(), client=client)

    async def test_a_failed_fetch_leaves_nothing_behind(self, app) -> None:
        """Neither a row promising bytes that were never stored, nor a temp file.

        A row without an object is the worse of the two: every later request
        becomes a redirect to nothing.
        """
        calls: list[str] = []
        storage = get_storage()
        async with get_db_session() as db, _fake_upstream(calls=calls, status=500) as client:
            with pytest.raises(UpstreamError):
                await fetch_and_cache(db, storage, _artifact(), client=client)

        async with get_db_session() as db:
            assert await lookup(db, "pypi", "flask", _artifact().filename) is None


class TestConcurrency:
    async def test_two_replicas_racing_a_cold_package_both_succeed(self, app) -> None:
        """Nothing coordinates the two, and nothing needs to.

        Both fetch — wasteful exactly once — but the bytes are identical, so the
        loser's unique violation resolves to the winner's row rather than
        surfacing as a 500 in somebody's install.
        """
        import asyncio

        storage = get_storage()
        calls: list[str] = []

        async def one():
            async with get_db_session() as db, _fake_upstream(calls=calls) as client:
                return await get_or_fetch(db, storage, _artifact(), client=client)

        first, second = await asyncio.gather(one(), one(), return_exceptions=True)
        for outcome in (first, second):
            assert not isinstance(outcome, Exception), outcome

        async with get_db_session() as db:
            count = await db.scalar(select(func.count()).select_from(CachedPackageFile))
        assert count == 1


class TestSealing:
    async def test_a_miss_on_a_sealed_node_names_the_setting(self, app, monkeypatch) -> None:
        """The operator has to be able to tell this from a network fault."""
        from terrapod.config import settings

        monkeypatch.setattr(settings.registry, "cache_only", True)
        storage = get_storage()

        async with get_db_session() as db:
            with pytest.raises(SealedError) as exc:
                await fetch_and_cache(db, storage, _artifact())

        assert "cache_only" in str(exc.value)

    async def test_an_already_cached_package_still_serves_when_sealed(
        self, app, monkeypatch
    ) -> None:
        """Sealing forbids reaching upstream, not using what is already here."""
        from terrapod.config import settings

        storage = get_storage()
        calls: list[str] = []
        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            await fetch_and_cache(db, storage, _artifact(), client=client)

        monkeypatch.setattr(settings.registry, "cache_only", True)
        async with get_db_session() as db:
            record = await get_or_fetch(db, storage, _artifact())

        assert await storage.get(record.storage_key) == WHEEL


class TestRetention:
    async def test_the_sweep_evicts_by_last_access(self, app) -> None:
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import update

        from terrapod.services.artifact_retention_service import _cleanup_package_cache

        storage = get_storage()
        calls: list[str] = []
        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            fresh = await fetch_and_cache(db, storage, _artifact("fresh-1.0.whl"), client=client)
            stale = await fetch_and_cache(db, storage, _artifact("stale-1.0.whl"), client=client)
            stale_id, stale_key, fresh_id = stale.id, stale.storage_key, fresh.id

        async with get_db_session() as db:
            await db.execute(
                update(CachedPackageFile)
                .where(CachedPackageFile.id == stale_id)
                .values(last_accessed_at=datetime.now(UTC) - timedelta(days=60))
            )

        async with get_db_session() as db:
            deleted = await _cleanup_package_cache(db, storage, retention_days=30, batch_size=100)

        assert deleted == 1
        assert not await storage.exists(stale_key)
        async with get_db_session() as db:
            assert await db.get(CachedPackageFile, fresh_id) is not None
            assert await db.get(CachedPackageFile, stale_id) is None
