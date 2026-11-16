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
from terrapod.services.package_cache import goproxy, pulumi_plugins
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
        """`go` and `pulumi` are deliberately outside `_DIGEST_REQUIRED` -- their
        material lives at a separate upstream. Until that is wired they must keep
        working, or the gate takes the cache down rather than making it safer.

        The ecosystem string is `goproxy.ECOSYSTEM`, NOT the module name. They
        differ (`"go"` vs `"goproxy"`), the gate matches on the former, and a
        fixture naming the latter tests an ecosystem no production code produces
        -- so it would keep passing after `go` joined the required set, which is
        the one regression this test exists to catch. It said `"goproxy"` until
        2026-11-16 and could not have caught it.
        """
        calls: list[str] = []
        storage = get_storage()
        artifact = Artifact(
            ecosystem=goproxy.ECOSYSTEM,
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
        about the ecosystem, the other is an absence of information about the row.

        Uses the real `goproxy.ECOSYSTEM` rather than the module name, for the
        reason given on the sibling test above.
        """
        calls: list[str] = []
        storage = get_storage()
        artifact = Artifact(
            ecosystem=goproxy.ECOSYSTEM,
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


class TestPulumiPluginVerification:
    """Two publishers, two answers, and the split is who we can hold to account.

    `get.pulumi.com` publishes nothing beside a tarball, so a Pulumi-published
    plugin is cached and recorded `none-published`. Terrapod's own bridged plugin
    is the exception: we publish it, its release carries a `SHA256SUMS` manifest,
    and so an absent or incomplete manifest is a statement about OUR release
    rather than about an ecosystem that ships no digests. That one is refused.

    Both halves are here because either alone is satisfied by the wrong
    implementation. "Ours is verified" passes if the resolver were attached to
    every plugin -- which would refuse `random` and `aws` for every install, the
    damaging direction.
    """

    PLUGIN_BODY = b"not really a plugin tarball, but bytes are bytes" * 50
    PLUGIN_SHA = hashlib.sha256(PLUGIN_BODY).hexdigest()
    RELEASE = "https://release.test/download"
    UPSTREAM = "https://plugins.test/releases/plugins"

    @pytest.fixture(autouse=True)
    def _bases(self, monkeypatch):
        from terrapod.config import settings

        pulumi = settings.registry.package_cache.pulumi
        monkeypatch.setattr(pulumi, "first_party_url", self.RELEASE)
        monkeypatch.setattr(pulumi, "upstream", self.UPSTREAM)

    def _client(self, calls: list[str], *, manifest: str | None, body: bytes | None = None):
        """Serves the manifest at its own URL and the tarball everywhere else.

        One transport for both, deliberately: `fetch_and_cache` passes the SAME
        client to the resolver so the extra request shares its connection pool,
        and a test using two clients would not exercise that.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if request.url.path.endswith("_SHA256SUMS"):
                if manifest is None:
                    return httpx.Response(404, content=b"Not Found")
                return httpx.Response(200, content=manifest.encode())
            return httpx.Response(200, content=self.PLUGIN_BODY if body is None else body)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def _artifact(self, filename: str, *, base: str | None = None):
        parts = pulumi_plugins.parse_filename(filename)
        assert parts is not None, filename
        return pulumi_plugins.artifact_for(filename, parts, base=base)

    async def test_the_first_party_plugin_is_verified_against_its_release_manifest(
        self, app
    ) -> None:
        filename = "pulumi-resource-terrapod-v8.1.0-linux-amd64.tar.gz"
        manifest = f"{self.PLUGIN_SHA}  {filename}\n"
        calls: list[str] = []
        storage = get_storage()

        async with get_db_session() as db, self._client(calls, manifest=manifest) as client:
            record = await fetch_and_cache(db, storage, self._artifact(filename), client=client)

        assert record.attestation == "upstream-digest"
        assert record.digest == f"sha256:{self.PLUGIN_SHA}"
        assert await storage.get(record.storage_key) == self.PLUGIN_BODY
        assert any(url.endswith("_SHA256SUMS") for url in calls), calls

    async def test_an_absent_manifest_REFUSES_the_first_party_plugin(self, app) -> None:
        """Nothing cached and nothing recorded. The alternative -- admit it as
        `none-published` -- would mean a broken release of ours was
        indistinguishable in the ledger from an upstream that ships no digests.
        """
        filename = "pulumi-resource-terrapod-v8.1.1-linux-amd64.tar.gz"
        calls: list[str] = []
        storage = get_storage()

        async with get_db_session() as db, self._client(calls, manifest=None) as client:
            with pytest.raises(UpstreamError, match="no readable checksums manifest"):
                await fetch_and_cache(db, storage, self._artifact(filename), client=client)

        async with get_db_session() as db:
            assert await lookup(db, "pulumi", "pulumi-resource-terrapod", filename) is None
        assert not await storage.exists(
            keys.package_cache_key("pulumi", "pulumi-resource-terrapod", filename)
        )

    async def test_a_manifest_NOT_LISTING_this_platform_REFUSES_it(self, app) -> None:
        """A partial release: the manifest is there and this asset is not in it."""
        filename = "pulumi-resource-terrapod-v8.1.2-windows-arm64.tar.gz"
        other = "pulumi-resource-terrapod-v8.1.2-linux-amd64.tar.gz"
        calls: list[str] = []
        storage = get_storage()

        async with (
            get_db_session() as db,
            self._client(calls, manifest=f"{self.PLUGIN_SHA}  {other}\n") as client,
        ):
            with pytest.raises(UpstreamError, match="is not listed in the checksums manifest"):
                await fetch_and_cache(db, storage, self._artifact(filename), client=client)

        async with get_db_session() as db:
            assert await lookup(db, "pulumi", "pulumi-resource-terrapod", filename) is None

    async def test_BYTES_THAT_DO_NOT_MATCH_the_manifest_are_refused_and_not_stored(
        self, app
    ) -> None:
        """The point of resolving a digest at all. A mismatch is either a
        corrupted transfer or a substituted artifact and there is no way to tell
        which from here, so neither the object nor the row is written."""
        filename = "pulumi-resource-terrapod-v8.1.3-linux-amd64.tar.gz"
        calls: list[str] = []
        storage = get_storage()
        manifest = f"{self.PLUGIN_SHA}  {filename}\n"

        async with (
            get_db_session() as db,
            self._client(calls, manifest=manifest, body=b"different bytes entirely") as client,
        ):
            with pytest.raises(DigestMismatch):
                await fetch_and_cache(db, storage, self._artifact(filename), client=client)

        async with get_db_session() as db:
            assert await lookup(db, "pulumi", "pulumi-resource-terrapod", filename) is None
        assert not await storage.exists(
            keys.package_cache_key("pulumi", "pulumi-resource-terrapod", filename)
        )

    async def test_a_PULUMI_PUBLISHED_plugin_caches_exactly_as_it_did_before(self, app) -> None:
        """The no-regression half. One upstream request, no manifest fetched, and
        the row records `none-published` rather than being left to look verified.
        """
        filename = "pulumi-resource-random-v4.16.3-linux-amd64.tar.gz"
        calls: list[str] = []
        storage = get_storage()

        async with get_db_session() as db, self._client(calls, manifest=None) as client:
            record = await fetch_and_cache(db, storage, self._artifact(filename), client=client)

        assert record.attestation == "none-published"
        assert record.digest == ""
        assert await storage.get(record.storage_key) == self.PLUGIN_BODY
        assert calls == [f"{self.UPSTREAM}/{filename}"], calls

    async def test_a_HIT_fetches_NEITHER_the_tarball_NOR_the_manifest(self, app) -> None:
        """Where the resolver is called is the whole of the sealed-node property.

        An `Artifact` is constructed on EVERY request including a cache hit, so a
        digest resolved during construction -- or anywhere above
        `lookup_present` -- would put an upstream round-trip on the hit path. The
        cost is not only latency: `cache_only` promises a sealed node serves hits
        with no egress at all, and that promise would be broken by the one
        ecosystem whose digest lives at a separate upstream.
        """
        filename = "pulumi-resource-terrapod-v8.2.0-linux-amd64.tar.gz"
        manifest = f"{self.PLUGIN_SHA}  {filename}\n"
        calls: list[str] = []
        storage = get_storage()

        async with get_db_session() as db, self._client(calls, manifest=manifest) as client:
            await get_or_fetch(db, storage, self._artifact(filename), client=client)
            await db.commit()
        after_miss = len(calls)
        assert after_miss == 2, f"a miss should fetch the manifest and the tarball: {calls}"

        async with get_db_session() as db, self._client(calls, manifest=manifest) as client:
            record = await get_or_fetch(db, storage, self._artifact(filename), client=client)
        assert record.attestation == "upstream-digest"
        assert len(calls) == after_miss, f"a hit reached upstream: {calls[after_miss:]}"

    async def test_SEALED_it_refuses_before_reaching_for_the_manifest(
        self, app, monkeypatch
    ) -> None:
        """A sealed node fetches neither the artifact nor its digest.

        `fetch_and_cache` refuses before anything else, so the resolver is never
        called -- which is what makes "the digest lives at a separate upstream"
        safe rather than a second thing that can fail while sealed. The assertion
        is on `calls` rather than on the exception: a `SealedError` raised AFTER a
        manifest request would satisfy a weaker test while having already
        reached out.
        """
        from terrapod.services.package_cache import substrate

        filename = "pulumi-resource-terrapod-v8.2.1-linux-amd64.tar.gz"
        calls: list[str] = []
        storage = get_storage()
        monkeypatch.setattr(substrate, "sealed", lambda: True)

        async with get_db_session() as db, self._client(calls, manifest="") as client:
            with pytest.raises(SealedError):
                await get_or_fetch(db, storage, self._artifact(filename), client=client)

        assert calls == [], f"a sealed node reached upstream: {calls}"

    async def test_a_MIRRORED_first_party_plugin_caches_exactly_as_it_did_before(self, app) -> None:
        """A warm `base` override is flat -- no version path segment -- so the
        manifest is not at a place we can derive. Requiring one would break
        warming from a mirror that holds the tarballs, which is config that works
        today, so the override admits and records honestly instead."""
        filename = "pulumi-resource-terrapod-v8.1.4-linux-amd64.tar.gz"
        calls: list[str] = []
        storage = get_storage()
        artifact = self._artifact(filename, base="https://mirror.test/p")

        async with get_db_session() as db, self._client(calls, manifest=None) as client:
            record = await fetch_and_cache(db, storage, artifact, client=client)

        assert record.attestation == "none-published"
        assert calls == [f"https://mirror.test/p/{filename}"], calls


class TestGoModulesRecordNonePublished:
    """Honest rather than verified, and the reason is in `goproxy.artifact_for`.

    The consumer already verifies with a stronger control: a Go program carries a
    committed `go.sum` and the `go` command enforces it against every module it
    downloads. A fetch-time digest would duplicate that against the checksum
    database, a third upstream a sealed deployment cannot reach.

    This drives `goproxy.artifact_for` rather than building an `Artifact` by
    hand, which is the difference from the sibling attestation test above it: a
    hand-built fixture says nothing about what the producer produces, so a
    `digest_resolver` quietly added there would go unnoticed.
    """

    @pytest.mark.parametrize("suffix", [".info", ".mod", ".zip"])
    async def test_every_immutable_file_records_it(self, app, suffix: str, monkeypatch) -> None:
        calls: list[str] = []
        storage = get_storage()
        monkeypatch.setattr(goproxy, "upstream_base", lambda: "https://goupstream.test")
        artifact = goproxy.artifact_for("example.com/attested", "v3.0.0", suffix)

        async with get_db_session() as db, _fake_upstream(calls=calls) as client:
            record = await fetch_and_cache(db, storage, artifact, client=client)

        assert record.attestation == "none-published"
        assert record.digest == ""
        # One request: no second upstream is consulted for a digest, which is the
        # whole of the decision recorded on `goproxy.artifact_for`.
        assert len(calls) == 1, calls
