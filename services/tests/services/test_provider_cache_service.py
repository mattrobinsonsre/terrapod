"""Tests for the provider network-mirror cache service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from terrapod.services.provider_cache_service import fetch_and_cache_single_platform


class TestConcurrentCacheMissRace:
    """Two concurrent cache-miss callers (typical when `tofu init` downloads
    several providers in parallel against an empty cache) both stream the
    binary into object storage, then both try to INSERT the
    cached_provider_packages row. The unique constraint on
    (hostname, namespace, type, version, os, arch) catches the second one;
    the service swallows the IntegrityError and falls back to serving from
    the row the winner just inserted, rather than letting the 5xx bubble out
    to the runner (which would otherwise fail `tofu init`).

    Regression for the data-pipelines-dev-us1 run that 500'd on
    hashicorp/dns + clickhouse/clickhouse + hashicorp/aws cache misses.
    """

    @pytest.mark.asyncio
    @patch("terrapod.services.provider_cache_service._get_cached_metadata", new_callable=AsyncMock)
    @patch(
        "terrapod.services.provider_cache_service._fetch_platform_download", new_callable=AsyncMock
    )
    @patch("terrapod.services.provider_cache_service.httpx.AsyncClient")
    @patch("terrapod.services.provider_cache_service.HashingStream")
    async def test_integrity_error_on_flush_falls_back_to_presigned_get(
        self,
        mock_hashing_stream: MagicMock,
        mock_async_client: MagicMock,
        mock_fetch_download: AsyncMock,
        mock_get_cached_metadata: AsyncMock,
    ) -> None:
        # Empty Redis cache + upstream returns a download URL.
        mock_get_cached_metadata.return_value = None
        mock_fetch_download.return_value = {
            "download_url": "https://upstream/example.zip",
            "filename": "terraform-provider-dns_3.4.3_linux_amd64.zip",
        }

        # Streaming download: a single-pass HashingStream wraps the response.
        stream_obj = MagicMock()
        stream_obj.sha256_hex = "e2ecc873" * 8
        stream_obj.size = 12_345_678
        mock_hashing_stream.return_value = stream_obj

        # httpx.AsyncClient().stream() context manager + AsyncClient() context manager.
        client_ctx = AsyncMock()
        client_ctx.__aenter__.return_value = client_ctx
        client_ctx.__aexit__.return_value = None
        mock_async_client.return_value = client_ctx
        stream_ctx = AsyncMock()
        stream_resp = MagicMock()
        stream_resp.raise_for_status = MagicMock()
        stream_ctx.__aenter__.return_value = stream_resp
        stream_ctx.__aexit__.return_value = None
        client_ctx.stream = MagicMock(return_value=stream_ctx)

        # DB: simulate the unique-constraint violation on flush.
        db = AsyncMock()
        db.add = MagicMock()
        db.flush.side_effect = IntegrityError(
            "INSERT", {}, Exception("uq_cached_provider_packages")
        )

        storage = AsyncMock()
        storage.put_stream = AsyncMock()
        presigned = MagicMock()
        presigned.url = "https://example/presigned/dns.zip"
        storage.presigned_get_url = AsyncMock(return_value=presigned)

        url, _h1 = await fetch_and_cache_single_platform(
            db,
            storage,
            hostname="registry.opentofu.org",
            namespace="hashicorp",
            type_="dns",
            version="3.4.3",
            os_="linux",
            arch="amd64",
        )

        # The race is handled: presigned URL is returned, not propagated as 500.
        assert url == "https://example/presigned/dns.zip"
        db.rollback.assert_awaited_once()


def _stream_mock(data: bytes, chunk_size: int = 64 * 1024):
    """Return an async generator yielding `data` in chunks — shaped
    to slot into `storage.get_stream = MagicMock(return_value=...)`.
    Used by the h1-backfill tests because the production path streams
    from storage to a tempfile (constant memory) rather than loading
    the whole archive via `storage.get`."""

    async def _gen():
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    return _gen()


class TestH1Backfill:
    """When a cached_provider_packages row has empty h1_hash (e.g.
    pre-h1-tracking, or h1 compute failed at ingest), the mirror should
    compute h1 from the cached archive on the next request and persist
    it — eliminating the runner's fallback to `tofu providers lock` for
    that provider. The backfill streams from storage to a tempfile so
    large archives don't OOM the API.
    """

    @pytest.mark.asyncio
    @patch("terrapod.services.provider_cache_service._get_cached_metadata", new_callable=AsyncMock)
    async def test_empty_h1_backfilled_from_archive_bytes(
        self,
        mock_get_cached_metadata: AsyncMock,
    ) -> None:
        # Build a valid provider zip so the h1 compute succeeds.
        import io
        import zipfile

        from terrapod.services.provider_cache_service import get_or_fetch_platforms

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("terraform-provider-null_v3.3.0", b"binary contents")
        archive_bytes = buf.getvalue()

        # DB entry with empty h1.
        entry = MagicMock()
        entry.id = "cpp-1"
        entry.filename = "terraform-provider-null_3.3.0_linux_amd64.zip"
        entry.os = "linux"
        entry.arch = "amd64"
        entry.shasum = "0123456789abcdef" * 4
        entry.h1_hash = ""  # the backfill trigger

        db = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [entry]
        db.execute = AsyncMock(return_value=result)
        db.flush = AsyncMock()

        storage = MagicMock()
        storage.exists = AsyncMock(return_value=True)
        storage.presigned_get_url = AsyncMock(return_value=MagicMock(url="https://x/p.zip"))
        storage.get_stream = MagicMock(return_value=_stream_mock(archive_bytes))
        storage.get = AsyncMock()  # should NOT be called — backfill streams

        # No upstream metadata fetch needed when only a cached platform is queried.
        mock_get_cached_metadata.return_value = None

        out = await get_or_fetch_platforms(
            db, storage, "registry.opentofu.org", "hashicorp", "null", "3.3.0"
        )

        # entry.h1_hash was assigned (the SQLAlchemy session would flush this
        # on commit; for the unit test it's enough that the in-memory value
        # changed — the object is tracked by the session).
        assert entry.h1_hash, "expected backfill to populate h1_hash"
        assert not entry.h1_hash.startswith("h1:"), "stored value omits the prefix"
        # And the served response includes the h1: hash.
        archive = out["archives"]["linux_amd64"]
        h1_entries = [h for h in archive["hashes"] if h.startswith("h1:")]
        assert h1_entries, f"response missing h1 hash: {archive['hashes']}"
        # Backfill streamed the archive (not the bytes-loading `storage.get`).
        storage.get_stream.assert_called_once()
        storage.get.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("terrapod.services.provider_cache_service._get_cached_metadata", new_callable=AsyncMock)
    async def test_existing_h1_not_recomputed(
        self,
        mock_get_cached_metadata: AsyncMock,
    ) -> None:
        """The hot path: rows with an h1_hash already stored serve it
        without re-reading the archive."""
        from terrapod.services.provider_cache_service import get_or_fetch_platforms

        entry = MagicMock()
        entry.id = "cpp-1"
        entry.filename = "terraform-provider-null_3.3.0_linux_amd64.zip"
        entry.os = "linux"
        entry.arch = "amd64"
        entry.shasum = "0123456789abcdef" * 4
        entry.h1_hash = "preexisting-h1-from-db"

        db = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [entry]
        db.execute = AsyncMock(return_value=result)

        storage = MagicMock()
        storage.exists = AsyncMock(return_value=True)
        storage.presigned_get_url = AsyncMock(return_value=MagicMock(url="https://x/p.zip"))
        storage.get = AsyncMock()  # should NOT be called
        storage.get_stream = MagicMock()  # should NOT be called either

        mock_get_cached_metadata.return_value = None

        out = await get_or_fetch_platforms(
            db, storage, "registry.opentofu.org", "hashicorp", "null", "3.3.0"
        )

        storage.get.assert_not_awaited()
        storage.get_stream.assert_not_called()
        assert "h1:preexisting-h1-from-db" in out["archives"]["linux_amd64"]["hashes"]


class TestSelfHostedRegistryTier:
    """Tier-0: a self-hostname request for a registered provider is served
    from the registry tables (not the cache tables, not upstream). This
    is what makes `terrapod.example.com/default/terrapod` resolvable via
    the mirror so the runner's lock-extender can splice h1 into
    .terraform.lock.hcl without a `tofu providers lock` fallback.
    """

    def _make_provider_zip(self) -> bytes:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("terraform-provider-terrapod_v0.33.0", b"binary contents")
        return buf.getvalue()

    @pytest.mark.asyncio
    @patch("terrapod.services.provider_cache_service.settings")
    async def test_self_hostname_registry_hit_returns_tier0(
        self,
        mock_settings: MagicMock,
    ) -> None:
        from terrapod.services.provider_cache_service import get_or_fetch_platforms

        # Settings: external_url's host matches the request hostname.
        mock_settings.external_url = "https://terrapod.example.com"
        mock_settings.registry.provider_cache.platforms = [
            {"os": "linux", "arch": "amd64"},
        ]

        # A registered provider/version/platform with non-empty h1.
        platform = MagicMock()
        platform.os = "linux"
        platform.arch = "amd64"
        platform.shasum = "deadbeef" * 8
        platform.upload_status = "uploaded"
        platform.h1_hash = "preexisting-h1"

        prov_version = MagicMock()
        prov_version.platforms = [platform]

        db = MagicMock()
        result = MagicMock()
        result.scalars.return_value.first.return_value = prov_version
        db.execute = AsyncMock(return_value=result)
        db.flush = AsyncMock()

        storage = MagicMock()
        storage.exists = AsyncMock(return_value=True)
        storage.presigned_get_url = AsyncMock(return_value=MagicMock(url="https://example/p.zip"))
        storage.get = AsyncMock()  # should NOT be called — h1 is present
        storage.get_stream = MagicMock()  # likewise

        out = await get_or_fetch_platforms(
            db, storage, "terrapod.example.com", "default", "terrapod", "0.33.0"
        )

        # Tier-0 served: zh + h1 hashes, preexisting h1 not recomputed.
        archive = out["archives"]["linux_amd64"]
        assert "zh:" + ("deadbeef" * 8) in archive["hashes"]
        assert "h1:preexisting-h1" in archive["hashes"]
        storage.get.assert_not_awaited()
        storage.get_stream.assert_not_called()

    @pytest.mark.asyncio
    @patch("terrapod.services.provider_cache_service.settings")
    async def test_self_hostname_registry_lazy_h1_backfill(
        self,
        mock_settings: MagicMock,
    ) -> None:
        from terrapod.services.provider_cache_service import get_or_fetch_platforms

        mock_settings.external_url = "https://terrapod.example.com"
        mock_settings.registry.provider_cache.platforms = []

        # Empty h1 — should be computed from the uploaded bytes.
        platform = MagicMock()
        platform.os = "linux"
        platform.arch = "amd64"
        platform.shasum = "cafef00d" * 8
        platform.upload_status = "uploaded"
        platform.h1_hash = ""

        prov_version = MagicMock()
        prov_version.platforms = [platform]

        db = MagicMock()
        result = MagicMock()
        result.scalars.return_value.first.return_value = prov_version
        db.execute = AsyncMock(return_value=result)
        db.flush = AsyncMock()

        archive_bytes = self._make_provider_zip()

        storage = MagicMock()
        storage.exists = AsyncMock(return_value=True)
        storage.presigned_get_url = AsyncMock(return_value=MagicMock(url="https://example/p.zip"))
        storage.get_stream = MagicMock(return_value=_stream_mock(archive_bytes))
        storage.get = AsyncMock()  # should NOT be called — backfill streams

        out = await get_or_fetch_platforms(
            db, storage, "terrapod.example.com", "default", "terrapod", "0.33.0"
        )

        # h1 was computed and persisted on the row.
        assert platform.h1_hash, "expected lazy backfill to populate h1_hash"
        assert not platform.h1_hash.startswith("h1:")

        # Response carries both zh and h1.
        archive = out["archives"]["linux_amd64"]
        h1_entries = [h for h in archive["hashes"] if h.startswith("h1:")]
        assert h1_entries, f"response missing h1 hash: {archive['hashes']}"
        # Backfill streamed (constant-memory path), did not load the
        # whole archive into RAM via storage.get.
        storage.get_stream.assert_called_once()
        storage.get.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("terrapod.services.provider_cache_service._get_cached_metadata", new_callable=AsyncMock)
    @patch("terrapod.services.provider_cache_service.settings")
    async def test_non_self_hostname_falls_through_to_tier1(
        self,
        mock_settings: MagicMock,
        mock_get_cached_metadata: AsyncMock,
    ) -> None:
        """Upstream-hostname requests must NOT consult the registry tables —
        e.g. `registry.opentofu.org/hashicorp/aws` is not ours."""
        from terrapod.services.provider_cache_service import get_or_fetch_platforms

        mock_settings.external_url = "https://terrapod.example.com"
        mock_settings.registry.provider_cache.platforms = []
        mock_settings.registry.provider_cache.warm_on_first_request = False

        # Tier-1 DB: empty.
        db = MagicMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)

        storage = MagicMock()

        mock_get_cached_metadata.return_value = None

        out = await get_or_fetch_platforms(
            db, storage, "registry.opentofu.org", "hashicorp", "aws", "5.0.0"
        )

        # Only one DB query — the Tier-1 cached_provider_packages select.
        # Tier-0 did NOT fire (would have been a separate query).
        assert db.execute.await_count == 1
        # Empty mirror response (no cached binaries, no upstream warm).
        assert out["archives"] == {}

    @pytest.mark.asyncio
    @patch("terrapod.services.provider_cache_service._get_cached_metadata", new_callable=AsyncMock)
    @patch("terrapod.services.provider_cache_service.settings")
    async def test_self_hostname_unknown_version_falls_through(
        self,
        mock_settings: MagicMock,
        mock_get_cached_metadata: AsyncMock,
    ) -> None:
        """Self-hostname request for a provider we don't have: Tier-0
        returns None, then standard tiers run (and also find nothing
        since the hostname isn't in upstream_registries)."""
        from terrapod.services.provider_cache_service import get_or_fetch_platforms

        mock_settings.external_url = "https://terrapod.example.com"
        mock_settings.registry.provider_cache.platforms = []
        mock_settings.registry.provider_cache.warm_on_first_request = False
        mock_settings.registry.provider_cache.upstream_registries = []

        # Tier-0 query returns no version; Tier-1 query also returns empty.
        db = MagicMock()
        tier0_result = MagicMock()
        tier0_result.scalars.return_value.first.return_value = None
        tier1_result = MagicMock()
        tier1_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(side_effect=[tier0_result, tier1_result])

        storage = MagicMock()

        mock_get_cached_metadata.return_value = None

        out = await get_or_fetch_platforms(
            db, storage, "terrapod.example.com", "default", "nonexistent", "1.0.0"
        )

        # Both Tier-0 and Tier-1 queries fired.
        assert db.execute.await_count == 2
        assert out["archives"] == {}
