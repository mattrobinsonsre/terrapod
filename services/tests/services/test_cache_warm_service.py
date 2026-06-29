"""Tests for cache_warm_service — the shared warm routine behind the
declarative manifest (startup trigger) and the bulk-warm admin endpoint."""

from unittest.mock import AsyncMock, patch

import pytest

from terrapod.config import WarmBinaryEntry, WarmPlatform, WarmProviderEntry
from terrapod.services import cache_warm_service
from terrapod.services.cache_warm_service import warm_from_manifest


def _db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


class TestWarmFromManifest:
    async def test_warms_each_binary_platform_default_platforms(self):
        db, storage = _db(), AsyncMock()
        with patch.object(
            cache_warm_service.binary_cache_service, "warm_binary", new_callable=AsyncMock
        ) as mock_warm:
            summary = await warm_from_manifest(
                db, storage, [WarmBinaryEntry(tool="tofu", version="1.9.0")], []
            )
        # Empty platforms → default linux/amd64 + linux/arm64 = 2 warm calls.
        assert mock_warm.await_count == 2
        assert summary.total == 2
        assert summary.succeeded == 2
        assert summary.failed == 0
        # Per-entry commit on success.
        assert db.commit.await_count == 2

    async def test_explicit_platforms_respected(self):
        db, storage = _db(), AsyncMock()
        with patch.object(
            cache_warm_service.binary_cache_service, "warm_binary", new_callable=AsyncMock
        ) as mock_warm:
            await warm_from_manifest(
                db,
                storage,
                [
                    WarmBinaryEntry(
                        tool="terraform",
                        version="1.12.0",
                        platforms=[WarmPlatform(os="darwin", arch="arm64")],
                    )
                ],
                [],
            )
        assert mock_warm.await_count == 1
        _, _, tool, version, os_, arch = mock_warm.await_args.args
        assert (tool, version, os_, arch) == ("terraform", "1.12.0", "darwin", "arm64")

    async def test_one_failure_does_not_abort_batch(self):
        db, storage = _db(), AsyncMock()
        # First platform fails, second succeeds — both reported, batch continues.
        with patch.object(
            cache_warm_service.binary_cache_service,
            "warm_binary",
            new_callable=AsyncMock,
            side_effect=[RuntimeError("upstream 404"), "ok-url"],
        ):
            summary = await warm_from_manifest(
                db, storage, [WarmBinaryEntry(tool="tofu", version="1.9.0")], []
            )
        assert summary.total == 2
        assert summary.succeeded == 1
        assert summary.failed == 1
        assert "upstream 404" in [r.error for r in summary.results if not r.ok][0]
        # Failure rolled back; success committed.
        assert db.rollback.await_count == 1
        assert db.commit.await_count == 1

    async def test_provider_uses_default_platforms_and_coordinates(self):
        db, storage = _db(), AsyncMock()
        with patch.object(
            cache_warm_service.provider_cache_service,
            "fetch_and_cache_single_platform",
            new_callable=AsyncMock,
            return_value=("url", "h1hash"),
        ) as mock_fetch:
            summary = await warm_from_manifest(
                db,
                storage,
                [],
                [WarmProviderEntry(source="registry.terraform.io/hashicorp/aws", version="5.60.0")],
            )
        # Default provider platforms = 2 (linux/amd64 + linux/arm64).
        assert mock_fetch.await_count == 2
        # Coordinates split from source.
        _, _, hostname, namespace, type_, version, _os, _arch = mock_fetch.await_args.args
        assert (hostname, namespace, type_, version) == (
            "registry.terraform.io",
            "hashicorp",
            "aws",
            "5.60.0",
        )
        assert summary.succeeded == 2

    async def test_disabled_binary_cache_marks_failed_not_skipped(self):
        db, storage = _db(), AsyncMock()
        with patch.object(
            cache_warm_service.binary_cache_service, "warm_binary", new_callable=AsyncMock
        ) as mock_warm:
            with patch.object(cache_warm_service.settings.registry.binary_cache, "enabled", False):
                summary = await warm_from_manifest(
                    db, storage, [WarmBinaryEntry(tool="tofu", version="1.9.0")], []
                )
        mock_warm.assert_not_called()
        assert summary.failed == 2
        assert all("disabled" in r.error for r in summary.results)


class TestWarmEntryValidation:
    def test_valid_provider_source_coordinates(self):
        e = WarmProviderEntry(source="registry.terraform.io/hashicorp/aws", version="5.0.0")
        assert e.coordinates == ("registry.terraform.io", "hashicorp", "aws")

    @pytest.mark.parametrize("bad", ["aws", "hashicorp/aws", "a/b/c/d", "a//c"])
    def test_invalid_provider_source_rejected(self, bad):
        with pytest.raises(ValueError):
            WarmProviderEntry(source=bad, version="5.0.0")
