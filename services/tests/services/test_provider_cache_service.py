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


class TestH1Backfill:
    """When a cached_provider_packages row has empty h1_hash (e.g.
    pre-h1-tracking, or h1 compute failed at ingest), the mirror should
    compute h1 from the cached archive bytes on the next request and
    persist it — eliminating the runner's fallback to `tofu providers
    lock` for that provider.
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
        storage.get = AsyncMock(return_value=archive_bytes)

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
        # storage.get was called exactly once for backfill.
        storage.get.assert_awaited_once()

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

        mock_get_cached_metadata.return_value = None

        out = await get_or_fetch_platforms(
            db, storage, "registry.opentofu.org", "hashicorp", "null", "3.3.0"
        )

        storage.get.assert_not_awaited()
        assert "h1:preexisting-h1-from-db" in out["archives"]["linux_amd64"]["hashes"]
