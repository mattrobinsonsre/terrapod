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


class TestConcurrentCacheMissRace:
    """Two concurrent cache-miss callers (typical when an empty cache faces
    a burst of runner starts) both stream the binary into object storage,
    then both try to INSERT the cached_binaries row. The unique constraint
    catches the second one; the service swallows the IntegrityError and
    falls back to serving from the row the winner just inserted, rather
    than letting the 5xx bubble out to the runner.
    """

    @pytest.mark.asyncio
    @patch("terrapod.services.binary_cache_service._fetch_and_store_binary", new_callable=AsyncMock)
    @patch("terrapod.services.binary_cache_service._get_cached", new_callable=AsyncMock)
    @patch("terrapod.services.binary_cache_service.settings")
    async def test_integrity_error_on_flush_falls_back_to_presigned_get(
        self,
        mock_settings: MagicMock,
        mock_get_cached: AsyncMock,
        mock_fetch: AsyncMock,
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        mock_settings.registry.binary_cache.allow_prerelease = "none"
        mock_settings.registry.binary_cache.verify = "off"  # not under test here (#607)
        # First call: cache miss (returns None). The second-fetcher path
        # below doesn't re-call _get_cached — the IntegrityError handler
        # falls straight through to presigning the row the winner wrote.
        mock_get_cached.return_value = None
        mock_fetch.return_value = ("deadbeef" * 8, 30_000_000)

        db = AsyncMock()
        # Simulate the unique-constraint violation when flushing the INSERT.
        db.flush.side_effect = IntegrityError("INSERT", {}, Exception("uq_cached_binaries"))

        storage = AsyncMock()
        presigned = MagicMock()
        presigned.url = "https://example/presigned"
        storage.presigned_get_url = AsyncMock(return_value=presigned)

        url = await get_or_cache_binary(db, storage, "tofu", "1.11.7", "linux", "arm64")
        assert url == "https://example/presigned"
        db.rollback.assert_awaited_once()


class TestTerragruntBinary:
    """Terragrunt is a third pull-through tool. Unlike terraform/tofu it ships
    a bare per-platform binary (not a zip), so the download URL has no `.zip`
    suffix and the stored object is octet-stream rather than application/zip.
    """

    @patch("terrapod.services.binary_cache_service.settings")
    def test_download_url_is_bare_github_binary(self, mock_settings: MagicMock) -> None:
        from terrapod.services.binary_cache_service import _terragrunt_download_url

        mock_settings.registry.binary_cache.terragrunt_mirror_url = (
            "https://github.com/gruntwork-io/terragrunt/releases/download"
        )
        url = _terragrunt_download_url("0.67.0", "linux", "amd64")
        assert url == (
            "https://github.com/gruntwork-io/terragrunt/releases/download/"
            "v0.67.0/terragrunt_linux_amd64"
        )
        assert not url.endswith(".zip")

    @pytest.mark.asyncio
    @patch("terrapod.services.binary_cache_service._fetch_and_store_binary", new_callable=AsyncMock)
    @patch("terrapod.services.binary_cache_service._get_cached", new_callable=AsyncMock)
    @patch("terrapod.services.binary_cache_service.settings")
    async def test_cache_miss_fetches_terragrunt_url_as_octet_stream(
        self,
        mock_settings: MagicMock,
        mock_get_cached: AsyncMock,
        mock_fetch: AsyncMock,
    ) -> None:
        mock_settings.registry.binary_cache.allow_prerelease = "none"
        mock_settings.registry.binary_cache.verify = "off"  # not under test here (#607)
        mock_settings.registry.binary_cache.terragrunt_mirror_url = (
            "https://github.com/gruntwork-io/terragrunt/releases/download"
        )
        mock_get_cached.return_value = None  # cache miss → fetch path
        mock_fetch.return_value = ("cafef00d" * 8, 45_000_000)

        db = AsyncMock()
        storage = AsyncMock()
        presigned = MagicMock()
        presigned.url = "https://example/presigned"
        storage.presigned_get_url = AsyncMock(return_value=presigned)

        url = await get_or_cache_binary(db, storage, "terragrunt", "0.67.0", "linux", "amd64")
        assert url == "https://example/presigned"

        # The fetch used the bare-binary terragrunt URL and stored it as
        # octet-stream (not the terraform/tofu zip content type).
        _args, kwargs = mock_fetch.call_args
        fetched_url = _args[2] if len(_args) > 2 else kwargs.get("url")
        assert fetched_url.endswith("/v0.67.0/terragrunt_linux_amd64")
        assert kwargs.get("content_type") == "application/octet-stream"

        # The recorded row is tagged tool=terragrunt.
        added = db.add.call_args[0][0]
        assert added.tool == "terragrunt"
        assert added.version == "0.67.0"


class TestTofuVersionResolution:
    """OpenTofu versions resolve via the official, non-rate-limited index
    (get.opentofu.org/tofu/api.json), not the GitHub releases API (#338)."""

    @patch("terrapod.services.binary_cache_service.arequest_with_retry", new_callable=AsyncMock)
    async def test_fetch_ids_parses_official_index(self, mock_req: AsyncMock) -> None:
        from terrapod.services.binary_cache_service import (
            _TOFU_VERSION_INDEX_URL,
            _fetch_tofu_version_ids,
        )

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(
            return_value={"versions": [{"id": "1.12.3"}, {"id": "1.12.0-rc1"}, {"id": "1.11.11"}]}
        )
        mock_req.return_value = resp

        ids = await _fetch_tofu_version_ids()

        assert ids == ["1.12.3", "1.12.0-rc1", "1.11.11"]
        # Must hit the official index, NOT api.github.com.
        called_url = mock_req.call_args[0][2]
        assert called_url == _TOFU_VERSION_INDEX_URL
        assert "api.github.com" not in called_url

    @patch("terrapod.services.binary_cache_service.settings")
    @patch(
        "terrapod.services.binary_cache_service._fetch_tofu_version_ids",
        new_callable=AsyncMock,
    )
    async def test_resolves_partial_to_highest_stable(
        self, mock_ids: AsyncMock, mock_settings: MagicMock
    ) -> None:
        from terrapod.services.binary_cache_service import _resolve_tofu_version

        mock_settings.registry.binary_cache.allow_prerelease = "none"
        mock_ids.return_value = ["1.12.0", "1.12.3", "1.12.1", "1.12.0-rc1", "1.11.11"]

        assert await _resolve_tofu_version("1.12") == "1.12.3"

    @patch("terrapod.services.binary_cache_service.settings")
    @patch(
        "terrapod.services.binary_cache_service._fetch_tofu_version_ids",
        new_callable=AsyncMock,
    )
    async def test_excludes_prereleases_when_policy_none(
        self, mock_ids: AsyncMock, mock_settings: MagicMock
    ) -> None:
        from terrapod.services.binary_cache_service import _resolve_tofu_version

        mock_settings.registry.binary_cache.allow_prerelease = "none"
        # Only pre-releases match → no stable → returns the partial unchanged.
        mock_ids.return_value = ["1.13.0-rc1", "1.13.0-beta1"]

        assert await _resolve_tofu_version("1.13") == "1.13"

    @patch("terrapod.services.binary_cache_service.settings")
    @patch(
        "terrapod.services.binary_cache_service._fetch_tofu_version_ids",
        new_callable=AsyncMock,
    )
    async def test_includes_rc_when_policy_rc(
        self, mock_ids: AsyncMock, mock_settings: MagicMock
    ) -> None:
        from terrapod.services.binary_cache_service import _resolve_tofu_version

        mock_settings.registry.binary_cache.allow_prerelease = "rc"
        mock_ids.return_value = ["1.13.0-rc1", "1.13.0-rc2"]

        assert await _resolve_tofu_version("1.13") == "1.13.0-rc2"
