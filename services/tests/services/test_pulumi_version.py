"""A Pulumi workspace pins its own CLI version (#1559).

Until this, the Pulumi version was one value for the whole deployment, in Helm,
and the runner asked an endpoint for it. Two workspaces could not sit on
different Pulumi versions the way two Terraform workspaces always could.

Pulumi is a CLI tool in the binary cache now, so it gets what the Terraform
family gets: a partial version resolved to the newest matching release, the
`allow_prerelease` policy, and a per-workspace pin carried on the run. What it
does not get is a signature, because Pulumi publishes none -- so these also pin
which verification switch governs it, which is the sort of thing that goes
wrong quietly.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.config import settings
from terrapod.services import binary_cache_service as bcs


def _index(*tags: str) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=[{"tag_name": t, "prerelease": "-" in t} for t in tags])
    return resp


class TestPulumiIsACliTool:
    def test_it_is_one(self):
        assert "pulumi" in bcs.CLI_TOOLS
        assert "pulumi" in bcs.VALID_TOOLS

    def test_it_is_no_longer_a_platform_tool(self):
        from terrapod.services.platform_tools import PLATFORM_TOOLS

        assert "pulumi" not in PLATFORM_TOOLS

    def test_its_asset_layout_is_still_described(self):
        # The tarball holding the language plugins is knowledge that had to stay
        # somewhere; the runner's unpack table is pinned equal to this.
        from terrapod.services.platform_tools import DESCRIBED_TOOLS, SPECS

        assert "pulumi" in DESCRIBED_TOOLS
        assert SPECS["pulumi"].member == "pulumi/pulumi"

    def test_it_is_verified_by_checksum_because_pulumi_signs_nothing(self):
        assert "pulumi" in bcs.CHECKSUM_ONLY_TOOLS


class TestPartialResolution:
    """The whole point: "3.208" has to mean "the newest 3.208.x"."""

    @patch("terrapod.services.binary_cache_service.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.binary_cache_service.settings")
    async def test_a_partial_resolves_to_the_newest_match(self, mock_settings, mock_req):
        mock_settings.registry.binary_cache.pulumi_version_index_url = "https://idx.test/releases"
        mock_settings.registry.binary_cache.allow_prerelease = "none"
        mock_req.return_value = _index("v3.208.0", "v3.208.2", "v3.208.1", "v3.209.0")

        assert await bcs._resolve_pulumi_version("3.208") == "3.208.2"

    @patch("terrapod.services.binary_cache_service.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.binary_cache_service.settings")
    async def test_the_configured_index_is_used(self, mock_settings, mock_req):
        # An operator behind a rate limit points this at their own mirror.
        mock_settings.registry.binary_cache.pulumi_version_index_url = "https://mirror.internal/p"
        mock_settings.registry.binary_cache.allow_prerelease = "none"
        mock_req.return_value = _index("v3.208.0")

        await bcs._fetch_pulumi_versions()

        assert mock_req.call_args[0][2] == "https://mirror.internal/p"
        assert "api.github.com" not in mock_req.call_args[0][2]

    @patch("terrapod.services.binary_cache_service.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.binary_cache_service.settings")
    async def test_no_match_returns_the_partial_rather_than_inventing_one(
        self, mock_settings, mock_req
    ):
        mock_settings.registry.binary_cache.pulumi_version_index_url = "https://idx.test/releases"
        mock_settings.registry.binary_cache.allow_prerelease = "none"
        mock_req.return_value = _index("v3.209.0")

        assert await bcs._resolve_pulumi_version("3.100") == "3.100"

    @patch("terrapod.services.binary_cache_service.arequest_with_retry", new_callable=AsyncMock)
    @patch("terrapod.services.binary_cache_service.settings")
    async def test_prereleases_are_excluded_by_the_default_policy(self, mock_settings, mock_req):
        mock_settings.registry.binary_cache.pulumi_version_index_url = "https://idx.test/releases"
        mock_settings.registry.binary_cache.allow_prerelease = "none"
        mock_req.return_value = _index("v3.208.0", "v3.208.1-rc.1")

        assert await bcs._fetch_pulumi_versions() == ["3.208.0"]


class TestPulumisDottedPreReleases:
    """Pulumi spells them `-rc.1`; HashiCorp spells them `-rc2`."""

    def test_a_dotted_suffix_orders_within_its_tier(self):
        # `int(".1")` used to raise, so every rc collapsed to the same key and
        # "the newest" was whichever the sort happened to leave last.
        ordered = sorted(["3.208.0-rc.2", "3.208.0-rc.1"], key=bcs._version_sort_key)
        assert ordered == ["3.208.0-rc.1", "3.208.0-rc.2"]

    def test_the_undotted_spelling_still_orders(self):
        ordered = sorted(["1.15.0-rc2", "1.15.0-rc1"], key=bcs._version_sort_key)
        assert ordered == ["1.15.0-rc1", "1.15.0-rc2"]

    def test_a_build_tag_after_the_number_does_not_defeat_it(self):
        ordered = sorted(
            ["3.208.0-alpha.2+abc123", "3.208.0-alpha.1+def456"], key=bcs._version_sort_key
        )
        assert ordered[0].startswith("3.208.0-alpha.1")

    def test_a_release_still_beats_its_own_prereleases(self):
        ordered = sorted(["3.208.0", "3.208.0-rc.9"], key=bcs._version_sort_key)
        assert ordered[-1] == "3.208.0"


class TestTheCliIsAvailableToEveryDeployment:
    """These used to be the two halves of an engine on/off switch (#1429).

    Withdrawn (#1986): the platform should just work, so neither the version
    listing nor the warm manifest asks whether an engine is allowed. What is
    left is the pair of properties that always mattered — every engine's
    versions resolve, and a sealed install is warmed with the binaries it will
    need, because a sealed install that forgets one has no way to get it.
    """

    async def test_listing_resolves_for_every_engine(self):
        for engine in ("terraform", "pulumi"):
            with patch.object(bcs, f"_fetch_{engine}_versions", AsyncMock(return_value=["9.9.9"])):
                with patch("terrapod.services.binary_cache_service._sealed", return_value=False):
                    assert "9.9.9" in await bcs.list_available_versions(engine)

    def test_the_warm_manifest_is_derived_from_the_configured_versions(self):
        """Derived rather than left to the manifest, so a sealed install cannot
        be missing a binary it has no way to fetch."""
        from terrapod.services import cache_warm_service

        entries = {e.tool: e.version for e in cache_warm_service.platform_tool_entries()}
        assert entries["pulumi"] == settings.default_pulumi_version

    def test_nothing_is_warmed_when_no_version_is_configured(self):
        """The only thing deciding is data — a version to fetch — not a switch.

        An operator who blanks the version is saying there is nothing to warm,
        which is different from saying an engine is not allowed.
        """
        from terrapod.services import cache_warm_service

        with patch.object(settings, "default_pulumi_version", ""):
            tools = [e.tool for e in cache_warm_service.platform_tool_entries()]
        assert "pulumi" not in tools


class TestServedSums:
    async def test_asking_for_a_signed_manifest_says_why_there_is_none(self):
        # A 404 would read as "the cache is cold"; this is a permanent fact
        # about the publisher.
        with pytest.raises(ValueError, match="no GPG-signed SHA256SUMS"):
            await bcs.get_or_cache_sums(MagicMock(), "pulumi", "3.208.0")


class TestWhichVersionARunResolves:
    """`execution_backend` says "tofu" on a Pulumi workspace, and always has."""

    def test_a_pulumi_workspace_resolves_against_pulumi(self):
        import inspect

        from terrapod.services import run_service

        source = inspect.getsource(run_service.create_run)
        assert 'version_tool = "pulumi" if workspace.engine == "pulumi"' in source
        assert "resolve_version(version_tool, requested_version)" in source


class TestTheRunCarriesTheVersion:
    def _env(self, options_kw=None, default="3.208"):
        from terrapod.engines.pulumi import PulumiRunOptions, PulumiStrategy

        options = PulumiRunOptions(stack="default/p/dev", **(options_kw or {}))
        cfg = SimpleNamespace(default_pulumi_version=default)
        return {e["name"]: e["value"] for e in PulumiStrategy().container_env(options, cfg)}

    def test_the_workspaces_version_reaches_the_container(self):
        assert self._env({"pulumi_version": "3.209.1"})["TP_PULUMI_VERSION"] == "3.209.1"

    def test_pinning_none_falls_back_to_the_deployment_default(self):
        assert self._env()["TP_PULUMI_VERSION"] == "3.208"

    def test_it_is_always_emitted(self):
        # It used to be conditional, and nothing read it, so every Pulumi run
        # silently used whatever Helm had pinned.
        assert "TP_PULUMI_VERSION" in self._env({"pulumi_version": ""}, default="")

    def test_the_engine_reads_the_canonical_attribute(self):
        from terrapod.engines.pulumi import PulumiStrategy

        options = PulumiStrategy().options_from_attrs(
            {"pulumi-stack": "default/p/dev", "engine-version": "3.209"}, "plan"
        )
        assert options.pulumi_version == "3.209"

    def test_the_runner_reads_it_off_the_env(self):
        from terrapod.runner.runner_config import RunnerConfig

        cfg = RunnerConfig.from_env(
            {"TP_PULUMI_VERSION": "3.209.1", "TP_API_URL": "http://api", "TP_RUN_ID": "r"}
        )
        assert cfg.pulumi_version == "3.209.1"
