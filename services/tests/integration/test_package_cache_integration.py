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
from sqlalchemy import func, select, update

from terrapod import http_retry
from terrapod.db.models import CachedPackageFile
from terrapod.db.session import get_db_session
from terrapod.services.package_cache.substrate import (
    Artifact,
    DigestMismatch,
    NotFoundUpstream,
    SealedError,
    UpstreamError,
    fetch_and_cache,
    get_or_fetch,
    lookup,
)
from terrapod.storage import get_storage, keys

pytestmark = pytest.mark.asyncio

WHEEL = b"not really a wheel, but bytes are bytes" * 100
WHEEL_SHA = hashlib.sha256(WHEEL).hexdigest()


def _fake_upstream(*, calls: list[str], status: int = 200, body: bytes = WHEEL):
    """An httpx client whose transport answers locally and counts requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fake_upstream_sequence(*, calls: list[str], statuses: list[int], body: bytes = WHEEL):
    """Return successive responses while retaining the usual request counter."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        status = statuses[min(len(calls) - 1, len(statuses) - 1)]
        return httpx.Response(status, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def zero_retry_delay(monkeypatch) -> None:
    monkeypatch.setattr(http_retry, "_backoff_seconds", lambda *_: 0.0)


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
        # Recomputed and COMPARED before storing (see TestVerification), then
        # recorded in upstream's own published form rather than ours.
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
        assert len(calls) == 1

    async def test_a_5xx_upstream_is_retried_before_the_package_is_cached(
        self, app, zero_retry_delay
    ) -> None:
        calls: list[str] = []
        storage = get_storage()
        async with (
            get_db_session() as db,
            _fake_upstream_sequence(calls=calls, statuses=[503, 200]) as client,
        ):
            record = await fetch_and_cache(db, storage, _artifact(), client=client)

        assert len(calls) == 2
        assert record.size == len(WHEEL)
        assert await storage.get(record.storage_key) == WHEEL

    async def test_a_500_upstream_is_an_upstream_error(self, app, zero_retry_delay) -> None:
        calls: list[str] = []
        storage = get_storage()
        async with get_db_session() as db, _fake_upstream(calls=calls, status=503) as client:
            with pytest.raises(UpstreamError):
                await fetch_and_cache(db, storage, _artifact(), client=client)
        assert len(calls) == http_retry.DEFAULT_RETRIES + 1

    async def test_a_failed_fetch_leaves_nothing_behind(self, app, zero_retry_delay) -> None:
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


class TestVerification:
    """The digest gate (#2589). Upstream's published digest is checked against the
    bytes upstream served, before anything is stored.

    These drive the real path with real storage, because the property that matters
    is not "it raises" but "it raises AND nothing was cached" -- and only real
    storage can answer the second half.
    """

    async def test_matching_bytes_are_cached(self, app) -> None:
        """The control. Without it a gate that refused everything would pass the
        three tests below and look correct."""
        calls: list[str] = []
        storage = get_storage()

        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            record = await fetch_and_cache(db, storage, _artifact(), client=client)

        assert await storage.get(record.storage_key) == WHEEL

    async def test_mismatched_bytes_are_refused_and_nothing_is_stored(self, app) -> None:
        """Upstream serves bytes that are not what it published a digest for.

        Either a corrupted transfer or a substituted artifact, and there is no way
        to tell which from here -- so the artifact must not be cached, and must not
        be half-stored for some later reader to find.
        """
        calls: list[str] = []
        storage = get_storage()
        artifact = _artifact("flask-3.0.0-mismatch.whl")

        async with (
            get_db_session() as db,
            _fake_upstream(calls=calls, body=b"other bytes") as client,
        ):
            with pytest.raises(DigestMismatch, match="published sha256:"):
                await fetch_and_cache(db, storage, artifact, client=client)

        async with get_db_session() as db:
            assert await lookup(db, "pypi", "flask", "flask-3.0.0-mismatch.whl") is None
        assert not await storage.exists(
            keys.package_cache_key("pypi", "flask", "flask-3.0.0-mismatch.whl")
        )

    async def test_an_absent_digest_is_refused_for_an_ecosystem_that_always_publishes_one(
        self, app
    ) -> None:
        """PyPI always publishes a sha256. An artifact arriving without one means
        the index was not the one we expect, which is a reason to stop rather than
        a reason to cache it unverified."""
        calls: list[str] = []
        storage = get_storage()
        artifact = Artifact(
            ecosystem="pypi",
            name="flask",
            version="3.0.0",
            filename="flask-3.0.0-nodigest.whl",
            upstream_url="https://upstream.test/flask-3.0.0-nodigest.whl",
            digest="",
        )

        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            with pytest.raises(DigestMismatch, match="published no digest"):
                await fetch_and_cache(db, storage, artifact, client=client)

        async with get_db_session() as db:
            assert await lookup(db, "pypi", "flask", "flask-3.0.0-nodigest.whl") is None

    async def test_npms_sri_integrity_is_understood(self, app) -> None:
        """The form a parser splitting on `:` alone cannot see at all."""
        import base64 as _b64

        calls: list[str] = []
        storage = get_storage()
        sri = "sha512-" + _b64.b64encode(hashlib.sha512(WHEEL).digest()).decode()
        artifact = Artifact(
            ecosystem="npm",
            name="left-pad",
            version="1.3.0",
            filename="left-pad-1.3.0.tgz",
            upstream_url="https://upstream.test/left-pad-1.3.0.tgz",
            digest=sri,
        )

        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            record = await fetch_and_cache(db, storage, artifact, client=client)

        assert record.digest == sri
        assert await storage.get(record.storage_key) == WHEEL

    async def test_an_ecosystem_with_no_published_digest_still_caches(self, app) -> None:
        """goproxy, nuget and pulumi_plugins are deliberately outside
        `_DIGEST_REQUIRED` -- their material lives at a separate upstream. Until
        that is wired they must keep working, or the gate takes the cache down
        rather than making it safer."""
        calls: list[str] = []
        storage = get_storage()
        artifact = Artifact(
            ecosystem="goproxy",
            name="example.com/m",
            version="v1.0.0",
            filename="v1.0.0.zip",
            upstream_url="https://upstream.test/v1.0.0.zip",
            digest="",
        )

        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            record = await fetch_and_cache(db, storage, artifact, client=client)

        assert await storage.get(record.storage_key) == WHEEL


class TestAttestation:
    """A row records HOW it was admitted, and an unattested one is re-verified.

    The population this exists for is every artifact cached before this cache
    verified anything: NULL means "a build that recorded nothing admitted this",
    and serving it would leave an operator unable to tell a verified artifact from
    one that merely arrived.
    """

    async def test_a_verified_artifact_records_the_digest_it_was_checked_against(self, app) -> None:
        calls: list[str] = []
        storage = get_storage()

        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            record = await fetch_and_cache(db, storage, _artifact("attest-ok.whl"), client=client)

        assert record.attestation == "upstream-digest"

    async def test_an_ecosystem_publishing_none_records_that_rather_than_nothing(self, app) -> None:
        """`none-published` and NULL must not be the same value: one is a declaration
        about the ecosystem, the other is an absence of information about the row."""
        calls: list[str] = []
        storage = get_storage()
        artifact = Artifact(
            ecosystem="goproxy",
            name="example.com/attest",
            version="v1.0.0",
            filename="v1.0.0.zip",
            upstream_url="https://upstream.test/v1.0.0.zip",
            digest="",
        )

        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            record = await fetch_and_cache(db, storage, artifact, client=client)

        assert record.attestation == "none-published"

    async def test_an_unattested_row_is_re_fetched_and_rewritten_exactly_once(self, app) -> None:
        """The property that matters, and the one a naive implementation gets wrong.

        The legacy row is still present, so `fetch_and_cache`'s insert is a
        duplicate and lands in the IntegrityError branch. Returning the existing row
        unchanged there would re-download the artifact on EVERY request and still
        serve a row nobody can vouch for -- so the third request below is the real
        assertion, not the second.
        """
        from sqlalchemy import update

        calls: list[str] = []
        storage = get_storage()
        artifact = _artifact("attest-legacy.whl")

        # Cache it, then forge the legacy state by clearing what was recorded.
        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            record = await fetch_and_cache(db, storage, artifact, client=client)
            await db.execute(
                update(CachedPackageFile)
                .where(CachedPackageFile.id == record.id)
                .values(attestation=None)
            )
            await db.commit()
        assert len(calls) == 1

        # Second request: a miss despite the row, so it is re-fetched and verified.
        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            again = await get_or_fetch(db, storage, artifact, client=client)
            await db.commit()
        assert len(calls) == 2, "an unattested row should have been treated as a miss"
        assert again.attestation == "upstream-digest", "the row was not rewritten"

        # Third request: now a hit. If the row had not been rewritten this would
        # fetch again, for ever.
        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            await get_or_fetch(db, storage, artifact, client=client)
        assert len(calls) == 2, "the transition must happen once, not on every request"

    async def test_an_unattested_row_whose_re_fetch_cannot_run_is_not_destroyed(
        self, app, monkeypatch
    ) -> None:
        """Sealed mode. The row must survive being unserveable.

        Deleting an unattested row on sight would be destructive here: the re-fetch
        cannot succeed while sealed, so the only record of a cached artifact would
        be gone for good rather than merely unserved until the operator re-warms.
        """
        from sqlalchemy import select

        from terrapod.services.package_cache import substrate

        calls: list[str] = []
        storage = get_storage()
        artifact = _artifact("attest-sealed.whl")

        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            record = await fetch_and_cache(db, storage, artifact, client=client)
            row_id = record.id
            await db.execute(
                update(CachedPackageFile)
                .where(CachedPackageFile.id == row_id)
                .values(attestation=None)
            )
            await db.commit()

        monkeypatch.setattr(substrate, "sealed", lambda: True)
        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            with pytest.raises(SealedError):
                await get_or_fetch(db, storage, artifact, client=client)

        async with get_db_session() as db:
            still = (
                await db.execute(select(CachedPackageFile).where(CachedPackageFile.id == row_id))
            ).scalar_one_or_none()
        assert still is not None, "the row was destroyed by a re-fetch that could not run"
