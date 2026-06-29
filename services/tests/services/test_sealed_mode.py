"""Tests for sealed (cache_only) mode — #606 part 3.

Covers the cache-backed fuzzy version matcher, the cache-miss → CacheOnlyError
behaviour, the provider mirror serving cached-only with no upstream, and the
artifact-retention sweeper skipping the caches when sealed.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import settings
from terrapod.services import (
    binary_cache_service,
    platform_provider_service,
    provider_cache_service,
)
from terrapod.services.binary_cache_service import _pick_cached_version
from terrapod.services.cache_errors import CacheOnlyError


def _sealed():
    """Patch cache_only=True on the shared settings singleton."""
    return patch.object(settings.registry, "cache_only", True)


def _fake_session(rows):
    """An async-context-manager get_db_session() yielding a db whose
    execute().all() returns the given 1-tuple rows."""
    db = AsyncMock()
    result = MagicMock()
    result.all = MagicMock(return_value=rows)
    db.execute = AsyncMock(return_value=result)

    @contextlib.asynccontextmanager
    async def _cm():
        yield db

    return _cm()


# ── Pure matcher (the cache-backed fuzzy version resolver) ────────────


class TestPickCachedVersion:
    # full list is newest-first, as _cached_versions returns it
    FULL = ["1.12.3", "1.12.1", "1.11.5", "1.11.0"]

    def test_partial_minor_picks_newest_patch(self):
        assert _pick_cached_version(self.FULL, "1.12") == "1.12.3"
        assert _pick_cached_version(self.FULL, "1.11") == "1.11.5"

    def test_empty_and_latest_pick_newest_overall(self):
        assert _pick_cached_version(self.FULL, "") == "1.12.3"
        assert _pick_cached_version(self.FULL, "latest") == "1.12.3"

    def test_exact_returned_as_is(self):
        assert _pick_cached_version(self.FULL, "1.11.0") == "1.11.0"
        # exact honoured even if not present — cache lookup errors later
        assert _pick_cached_version(self.FULL, "9.9.9") == "9.9.9"

    def test_no_cached_match_returns_none(self):
        assert _pick_cached_version(self.FULL, "1.99") is None
        assert _pick_cached_version([], "1.12") is None
        assert _pick_cached_version([], "") is None


# ── Cache-backed version list + resolution (sealed) ───────────────────


class TestSealedVersionResolution:
    async def test_cached_versions_distinct_sorted_with_shortcuts(self):
        db = AsyncMock()
        result = MagicMock()
        result.all = MagicMock(return_value=[("1.12.1",), ("1.12.3",), ("1.11.0",)])
        db.execute = AsyncMock(return_value=result)

        out = await binary_cache_service._cached_versions(db, "tofu")
        # shortcuts first (newest-first), then full versions newest-first
        assert out == ["1.12", "1.11", "1.12.3", "1.12.1", "1.11.0"]

    async def test_resolve_version_sealed_uses_cache(self):
        with (
            _sealed(),
            patch(
                "terrapod.db.session.get_db_session",
                return_value=_fake_session([("1.12.3",), ("1.12.1",)]),
            ),
        ):
            assert await binary_cache_service.resolve_version("terraform", "1.12") == "1.12.3"

    async def test_resolve_version_sealed_no_match_raises(self):
        with (
            _sealed(),
            patch(
                "terrapod.db.session.get_db_session",
                return_value=_fake_session([("1.12.3",)]),
            ),
        ):
            with pytest.raises(CacheOnlyError):
                await binary_cache_service.resolve_version("terraform", "1.99")

    async def test_list_available_versions_sealed_returns_cache(self):
        with (
            _sealed(),
            patch(
                "terrapod.db.session.get_db_session",
                return_value=_fake_session([("1.12.3",), ("1.11.0",)]),
            ),
        ):
            out = await binary_cache_service.list_available_versions("tofu")
        assert out == ["1.12", "1.11", "1.12.3", "1.11.0"]


# ── Cache miss in sealed mode → actionable error, no upstream ─────────


class TestSealedBinaryMiss:
    async def test_get_or_cache_binary_miss_raises_cache_only(self):
        db, storage = AsyncMock(), AsyncMock()
        with (
            _sealed(),
            patch.object(binary_cache_service, "_get_cached", new=AsyncMock(return_value=None)),
            patch.object(
                binary_cache_service, "_fetch_and_store_binary", new=AsyncMock()
            ) as mock_fetch,
        ):
            with pytest.raises(CacheOnlyError):
                await binary_cache_service.get_or_cache_binary(
                    db, storage, "terraform", "1.12.3", "linux", "amd64"
                )
        mock_fetch.assert_not_called()  # never touched upstream


# ── Provider mirror: cached-only, no upstream (sealed) ────────────────


class TestSealedProvider:
    async def test_get_or_fetch_platforms_sealed_no_upstream(self):
        db, storage = AsyncMock(), AsyncMock()
        empty = MagicMock()
        empty.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        db.execute = AsyncMock(return_value=empty)

        with (
            _sealed(),
            patch.object(
                provider_cache_service, "_get_cached_metadata", new=AsyncMock()
            ) as mock_meta,
            patch.object(
                provider_cache_service,
                "_fetch_and_cache_upstream_metadata",
                new=AsyncMock(),
            ) as mock_up,
        ):
            resp = await provider_cache_service.get_or_fetch_platforms(
                db, storage, "registry.terraform.io", "hashicorp", "aws", "5.60.0"
            )
        # Nothing cached in storage → no archives served, and crucially no
        # upstream metadata fetch or eager caching happened.
        assert resp["archives"] == {}
        mock_meta.assert_not_called()
        mock_up.assert_not_called()

    async def test_get_or_fetch_versions_sealed_uses_cache(self):
        db = AsyncMock()
        result = MagicMock()
        result.all = MagicMock(return_value=[("5.60.0",), ("5.59.0",)])
        db.execute = AsyncMock(return_value=result)

        with (
            _sealed(),
            patch.object(
                provider_cache_service, "_fetch_upstream_versions", new=AsyncMock()
            ) as mock_up,
        ):
            resp = await provider_cache_service.get_or_fetch_versions(
                db, "registry.terraform.io", "hashicorp", "aws"
            )
        assert set(resp["versions"].keys()) == {"5.60.0", "5.59.0"}
        mock_up.assert_not_called()

    async def test_fetch_and_cache_single_platform_sealed_raises(self):
        db, storage = AsyncMock(), AsyncMock()
        with _sealed(), pytest.raises(CacheOnlyError):
            await provider_cache_service.fetch_and_cache_single_platform(
                db, storage, "registry.terraform.io", "hashicorp", "aws", "5.60.0", "linux", "amd64"
            )


# ── Sealed: SHA256SUMS re-verify path + platform provider (no upstream) ──


class TestSealedSums:
    async def test_get_or_cache_sums_miss_raises_no_upstream(self):
        # Not persisted + sealed → CacheOnlyError, never constructs an httpx client.
        storage = AsyncMock()
        storage.exists = AsyncMock(return_value=False)
        with (
            _sealed(),
            patch.object(binary_cache_service.httpx, "AsyncClient") as mock_client,
        ):
            with pytest.raises(CacheOnlyError):
                await binary_cache_service.get_or_cache_sums(storage, "terraform", "1.12.3")
        mock_client.assert_not_called()


class TestSealedPlatformProvider:
    async def test_get_download_info_miss_raises_no_github(self):
        # The Terrapod provider itself must not be fetched from GitHub when sealed.
        storage = AsyncMock()
        storage.exists = AsyncMock(return_value=False)
        with (
            _sealed(),
            patch.object(
                platform_provider_service, "_fetch_and_cache_binary", new=AsyncMock()
            ) as mock_fetch,
        ):
            with pytest.raises(CacheOnlyError):
                await platform_provider_service.get_download_info(
                    storage, "0.49.0", "linux", "amd64"
                )
        mock_fetch.assert_not_called()


# ── Artifact retention skips caches when sealed ──────────────────────


class TestSealedRetentionSkip:
    async def test_cache_categories_skipped_when_sealed(self):
        # Enable retention with cache thresholds set, then seal; the binary +
        # provider cache cleanups must be skipped (un-refetchable artifacts).
        from terrapod.services import artifact_retention_service as ars

        called: list[str] = []

        async def _spy(db, storage, threshold, batch):  # noqa: ANN001
            return 0

        with (
            _sealed(),
            patch.object(ars.settings.artifact_retention, "enabled", True),
            patch.object(ars.settings.artifact_retention, "binary_cache_retention_days", 30),
            patch.object(ars.settings.artifact_retention, "provider_cache_retention_days", 30),
            patch.object(ars.settings.artifact_retention, "state_versions_keep", 0),
            patch.object(ars.settings.artifact_retention, "run_artifacts_retention_days", 0),
            patch.object(ars.settings.artifact_retention, "config_versions_retention_days", 0),
            patch.object(ars.settings.artifact_retention, "config_versions_keep", 0),
            patch.object(ars.settings.artifact_retention, "module_overrides_retention_days", 0),
            patch.object(ars.settings.vcs, "archive_cache_retention_days", 0),
            patch("terrapod.storage.get_storage", return_value=MagicMock()),
            patch.object(
                ars,
                "_cleanup_binary_cache",
                new=AsyncMock(side_effect=lambda *a, **k: called.append("binary") or 0),
            ),
            patch.object(
                ars,
                "_cleanup_provider_cache",
                new=AsyncMock(side_effect=lambda *a, **k: called.append("provider") or 0),
            ),
        ):
            await ars.artifact_retention_cycle()

        assert called == []  # both cache cleanups skipped under sealed mode
