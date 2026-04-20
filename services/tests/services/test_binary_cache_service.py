"""Tests for binary cache pre-release handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services.binary_cache_service import (
    _is_version_allowed,
    _parse_stability,
    _version_sort_key,
    get_or_cache_binary,
)


class TestParseStability:
    @pytest.mark.parametrize(
        "version,expected",
        [
            ("1.15.0", "stable"),
            ("1.14.8", "stable"),
            ("1.0.0", "stable"),
            ("1.15.0-rc1", "rc"),
            ("1.15.0-rc2", "rc"),
            ("1.12.0-beta1", "beta"),
            ("1.15.0-alpha1", "alpha"),
            ("1.15.0-alpha2", "alpha"),
            ("1.15.0-dev", "dev"),
        ],
    )
    def test_parses_tier(self, version: str, expected: str) -> None:
        assert _parse_stability(version) == expected


class TestIsVersionAllowed:
    """Policy naming: the value is the LEAST stable tier accepted."""

    @pytest.mark.parametrize(
        "policy,allowed,denied",
        [
            (
                "none",
                ["1.15.0", "1.14.8"],
                ["1.15.0-rc2", "1.15.0-beta1", "1.15.0-alpha1", "1.15.0-dev"],
            ),
            (
                "rc",
                ["1.15.0", "1.15.0-rc2"],
                ["1.15.0-beta1", "1.15.0-alpha1", "1.15.0-dev"],
            ),
            (
                "beta",
                ["1.15.0", "1.15.0-rc2", "1.12.0-beta1"],
                ["1.15.0-alpha1", "1.15.0-dev"],
            ),
            (
                "alpha",
                ["1.15.0", "1.15.0-rc2", "1.12.0-beta1", "1.15.0-alpha1"],
                ["1.15.0-dev"],
            ),
            (
                "dev",
                ["1.15.0", "1.15.0-rc2", "1.12.0-beta1", "1.15.0-alpha1", "1.15.0-dev"],
                [],
            ),
        ],
    )
    def test_policy_admits_expected_tiers(
        self, policy: str, allowed: list[str], denied: list[str]
    ) -> None:
        for v in allowed:
            assert _is_version_allowed(v, policy), f"{v} should be allowed by policy={policy}"
        for v in denied:
            assert not _is_version_allowed(v, policy), f"{v} should be denied by policy={policy}"

    def test_unknown_policy_falls_back_to_stable_only(self) -> None:
        assert _is_version_allowed("1.15.0", "bogus")
        assert not _is_version_allowed("1.15.0-rc2", "bogus")


class TestVersionSortKey:
    """Sort key orders versions semver-ish with stability rank applied."""

    def test_stable_before_rc_before_beta_before_alpha_before_dev(self) -> None:
        versions = [
            "1.15.0-dev",
            "1.15.0-alpha1",
            "1.15.0-alpha2",
            "1.15.0-beta1",
            "1.15.0-rc1",
            "1.15.0-rc2",
            "1.15.0",
        ]
        shuffled = list(reversed(versions))
        shuffled.sort(key=_version_sort_key)
        assert shuffled == versions

    def test_patch_versions_sort_above_pre_releases_of_next_minor(self) -> None:
        # 1.14.8 (stable) < 1.15.0-alpha1: higher minor beats stable
        got = sorted(["1.14.8", "1.15.0-alpha1"], key=_version_sort_key)
        assert got == ["1.14.8", "1.15.0-alpha1"]

    def test_all_stable_sort_in_patch_order(self) -> None:
        got = sorted(
            ["1.14.8", "1.14.10", "1.14.9", "1.15.0"],
            key=_version_sort_key,
        )
        assert got == ["1.14.8", "1.14.9", "1.14.10", "1.15.0"]

    def test_rc_numbers_within_tier_sort_by_integer(self) -> None:
        got = sorted(
            ["1.15.0-rc10", "1.15.0-rc2", "1.15.0-rc1"],
            key=_version_sort_key,
        )
        assert got == ["1.15.0-rc1", "1.15.0-rc2", "1.15.0-rc10"]

    def test_handles_bare_dev_without_number(self) -> None:
        # "-dev" with no trailing number should not blow up
        got = sorted(["1.15.0", "1.15.0-dev"], key=_version_sort_key)
        assert got == ["1.15.0-dev", "1.15.0"]


class TestGetOrCacheBinaryGatesPrereleases:
    """Integration-ish: confirm the policy is enforced at the service entry point."""

    @pytest.mark.asyncio
    @patch("terrapod.services.binary_cache_service.settings")
    async def test_rejects_rc_when_policy_is_none(self, mock_settings: MagicMock) -> None:
        mock_settings.registry.binary_cache.allow_prerelease = "none"
        db = AsyncMock()
        storage = AsyncMock()
        with pytest.raises(ValueError, match="Pre-release version"):
            await get_or_cache_binary(db, storage, "terraform", "1.15.0-rc2", "linux", "amd64")

    @pytest.mark.asyncio
    @patch("terrapod.services.binary_cache_service.settings")
    async def test_rejects_beta_when_policy_is_rc(self, mock_settings: MagicMock) -> None:
        mock_settings.registry.binary_cache.allow_prerelease = "rc"
        db = AsyncMock()
        storage = AsyncMock()
        with pytest.raises(ValueError, match="Pre-release version"):
            await get_or_cache_binary(db, storage, "tofu", "1.12.0-beta1", "linux", "amd64")

    @pytest.mark.asyncio
    @patch("terrapod.services.binary_cache_service._get_cached", new_callable=AsyncMock)
    @patch("terrapod.services.binary_cache_service.settings")
    async def test_accepts_rc_when_policy_is_rc(
        self,
        mock_settings: MagicMock,
        mock_get_cached: AsyncMock,
    ) -> None:
        # With policy=rc, a -rc version must pass the gate. Stub out the cache
        # lookup with a sentinel so the function short-circuits before any DB
        # or storage I/O.
        mock_settings.registry.binary_cache.allow_prerelease = "rc"
        sentinel = MagicMock()
        sentinel.last_accessed_at = None
        mock_get_cached.return_value = sentinel

        db = AsyncMock()
        storage = AsyncMock()
        presigned = MagicMock()
        presigned.url = "https://example/presigned"
        storage.presigned_get_url = AsyncMock(return_value=presigned)

        url = await get_or_cache_binary(db, storage, "terraform", "1.15.0-rc2", "linux", "amd64")
        assert url == "https://example/presigned"

    @pytest.mark.asyncio
    @patch("terrapod.services.binary_cache_service._get_cached", new_callable=AsyncMock)
    @patch("terrapod.services.binary_cache_service.settings")
    async def test_always_accepts_stable_regardless_of_policy(
        self,
        mock_settings: MagicMock,
        mock_get_cached: AsyncMock,
    ) -> None:
        mock_settings.registry.binary_cache.allow_prerelease = "none"
        sentinel = MagicMock()
        sentinel.last_accessed_at = None
        mock_get_cached.return_value = sentinel

        db = AsyncMock()
        storage = AsyncMock()
        presigned = MagicMock()
        presigned.url = "https://example/presigned"
        storage.presigned_get_url = AsyncMock(return_value=presigned)

        url = await get_or_cache_binary(db, storage, "terraform", "1.14.8", "linux", "amd64")
        assert url == "https://example/presigned"
