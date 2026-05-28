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

        url = await fetch_and_cache_single_platform(
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
