"""Platform-tool fetch + verification (#1208).

These three tools were baked into the published images until this landed, so the
tests worth having are the ones that pin what replaced that guarantee: the right
asset for a platform, the publisher's checksum actually checked, and a mismatch
refusing to cache. A silently-wrong asset name or a verification step that
passes on missing material would reintroduce exactly the exposure the change
was made to remove.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services.artifact_verification import VerificationError
from terrapod.services.platform_tools import (
    PLATFORM_TOOLS,
    SPECS,
    UnsupportedPlatformError,
    _expected_sha256,
    configured_version,
    download_url,
    verify_platform_tool,
)


def _resp(status: int = 200, text: str = ""):
    """A stand-in for the httpx response `arequest_with_retry` returns."""
    r = AsyncMock()
    r.status_code = status
    r.text = text
    return r


class TestDownloadURL:
    @pytest.mark.parametrize(
        "tool,version,os_,arch,expected",
        [
            (
                "opa",
                "1.19.0",
                "linux",
                "amd64",
                "https://github.com/open-policy-agent/opa/releases/download/"
                "v1.19.0/opa_linux_amd64_static",
            ),
            (
                "opa",
                "1.19.0",
                "linux",
                "arm64",
                "https://github.com/open-policy-agent/opa/releases/download/"
                "v1.19.0/opa_linux_arm64_static",
            ),
            (
                "trivy",
                "0.72.0",
                "linux",
                "amd64",
                "https://github.com/aquasecurity/trivy/releases/download/"
                "v0.72.0/trivy_0.72.0_Linux-64bit.tar.gz",
            ),
            (
                "trivy",
                "0.72.0",
                "linux",
                "arm64",
                "https://github.com/aquasecurity/trivy/releases/download/"
                "v0.72.0/trivy_0.72.0_Linux-ARM64.tar.gz",
            ),
            # Checkov comes from Terrapod's own releases, not Checkov's (#2066):
            # upstream stopped publishing checkov_linux_arm64.zip at 3.3.21, so
            # we build the linux assets from its tag and publish them ourselves.
            (
                "checkov",
                "3.3.8",
                "linux",
                "amd64",
                "https://github.com/mattrobinsonsre/terrapod/releases/download/"
                "3.3.8/checkov_linux_X86_64.zip",
            ),
            (
                "checkov",
                "3.3.8",
                "linux",
                "arm64",
                "https://github.com/mattrobinsonsre/terrapod/releases/download/"
                "3.3.8/checkov_linux_arm64.zip",
            ),
        ],
    )
    def test_matches_the_published_asset_names(
        self, tool: str, version: str, os_: str, arch: str, expected: str
    ) -> None:
        """Verified against the real release assets.

        Upstream naming is not consistent — trivy says "Linux-64bit", checkov
        "linux_X86_64" with a capital X, opa plain "linux_amd64" — so a
        plausible-looking guess here 404s at fetch time.
        """
        assert download_url(tool, version, os_, arch) == expected

    @pytest.mark.parametrize(
        "tool,os_,arch",
        [
            ("opa", "windows", "amd64"),
            ("opa", "linux", "arm"),
            ("trivy", "windows", "arm64"),
            ("checkov", "linux", "386"),
            # Platform tools are LINUX ONLY (#2066). The API and the runner
            # fetch these for themselves and both are linux, so a darwin entry
            # was a claim with no consumer — and claims with no consumer are
            # where the bugs were: checkov's table asserted a linux/arm64 asset
            # upstream had stopped publishing, and opa's asked for
            # `opa_darwin_amd64_static`, which exists on no version.
            ("opa", "darwin", "amd64"),
            ("opa", "darwin", "arm64"),
            ("trivy", "darwin", "amd64"),
            ("trivy", "darwin", "arm64"),
            ("checkov", "darwin", "amd64"),
            ("checkov", "darwin", "arm64"),
        ],
    )
    def test_rejects_a_platform_the_publisher_does_not_build(
        self, tool: str, os_: str, arch: str
    ) -> None:
        """Better than letting it 404 downstream where the cause is unclear."""
        with pytest.raises(UnsupportedPlatformError):
            download_url(tool, "1.0.0", os_, arch)

    def test_every_described_tool_has_a_spec(self) -> None:
        # DESCRIBED_TOOLS, not PLATFORM_TOOLS: pulumi's version became
        # per-workspace in #1559 but its asset layout is still written down
        # here, and the runner's unpack table is pinned equal to this one.
        from terrapod.services.platform_tools import DESCRIBED_TOOLS

        assert set(SPECS) == set(DESCRIBED_TOOLS)


class TestExpectedSha256:
    """Each publisher exposes its checksum somewhere different."""

    @pytest.mark.asyncio
    async def test_opa_reads_the_sibling_sha256_file(self) -> None:
        with patch(
            "terrapod.services.platform_tools.arequest_with_retry",
            AsyncMock(return_value=_resp(text="ABCD1234  opa_linux_amd64_static\n")),
        ) as req:
            assert await _expected_sha256(None, "opa", "1.19.0", "linux", "amd64") == "abcd1234"
        assert req.await_args.args[2].endswith("/v1.19.0/opa_linux_amd64_static.sha256")

    @pytest.mark.asyncio
    async def test_trivy_finds_its_asset_in_the_release_manifest(self) -> None:
        manifest = (
            "1111  trivy_0.72.0_Linux-ARM64.tar.gz\n"
            "BEEF  trivy_0.72.0_Linux-64bit.tar.gz\n"
            "2222  trivy_0.72.0_macOS-64bit.tar.gz\n"
        )
        with patch(
            "terrapod.services.platform_tools.arequest_with_retry",
            AsyncMock(return_value=_resp(text=manifest)),
        ):
            assert await _expected_sha256(None, "trivy", "0.72.0", "linux", "amd64") == "beef"

    @pytest.mark.asyncio
    async def test_trivy_refuses_when_its_asset_is_absent_from_the_manifest(self) -> None:
        """A manifest that doesn't mention the asset verifies nothing."""
        with patch(
            "terrapod.services.platform_tools.arequest_with_retry",
            AsyncMock(return_value=_resp(text="1111  something_else.tar.gz\n")),
        ):
            with pytest.raises(VerificationError, match="not listed"):
                await _expected_sha256(None, "trivy", "0.72.0", "linux", "amd64")

    @pytest.mark.asyncio
    async def test_pulumi_finds_its_asset_in_the_release_manifest(self) -> None:
        """Real manifest lines from the v3.208.0 release, trimmed to three."""
        manifest = (
            "ee2610  pulumi-v3.208.0-darwin-arm64.tar.gz\n"
            "4e421b  pulumi-v3.208.0-linux-arm64.tar.gz\n"
            "999999  pulumi-v3.208.0-linux-x64.tar.gz\n"
        )
        with patch(
            "terrapod.services.platform_tools.arequest_with_retry",
            AsyncMock(return_value=_resp(text=manifest)),
        ) as req:
            got = await _expected_sha256(None, "pulumi", "3.208.0", "linux", "arm64")
        assert got == "4e421b"
        # The tag carries a `v`, the manifest filename does not. Pinned because
        # getting it wrong 404s, and a 404 here fails closed as "unverifiable
        # artifact" rather than announcing itself as a URL typo.
        assert req.await_args.args[2].endswith("/v3.208.0/pulumi-3.208.0-checksums.txt")

    @pytest.mark.asyncio
    async def test_pulumi_refuses_when_its_asset_is_absent_from_the_manifest(self) -> None:
        with patch(
            "terrapod.services.platform_tools.arequest_with_retry",
            AsyncMock(return_value=_resp(text="1111  something_else.tar.gz\n")),
        ):
            with pytest.raises(VerificationError, match="not listed"):
                await _expected_sha256(None, "pulumi", "3.208.0", "linux", "arm64")

    @pytest.mark.asyncio
    async def test_pulumi_refuses_when_the_manifest_cannot_be_fetched(self) -> None:
        with patch(
            "terrapod.services.platform_tools.arequest_with_retry",
            AsyncMock(return_value=_resp(status=404)),
        ):
            with pytest.raises(VerificationError, match="could not fetch"):
                await _expected_sha256(None, "pulumi", "3.208.0", "linux", "arm64")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", sorted(PLATFORM_TOOLS))
    async def test_every_platform_tool_has_a_checksum_branch(self, tool: str) -> None:
        """Membership of PLATFORM_TOOLS is not enough to be fetchable.

        A tool needs three things and gains them in separate edits: an entry in
        the frozenset, a `download_url` branch, and a `_expected_sha256` branch.
        Pulumi shipped with the first two and not the third, so every Pulumi run
        downloaded its CLI successfully and was then refused at verification with
        "not a platform tool: 'pulumi'" — a message that reads like the frozenset
        is wrong when in fact only this function was.

        Asserting on that exact fall-through, rather than on success, is what
        makes this independent of how any one publisher serves its checksums.
        """
        with patch(
            "terrapod.services.platform_tools.arequest_with_retry",
            AsyncMock(return_value=_resp(status=500)),
        ):
            with pytest.raises(VerificationError) as excinfo:
                await _expected_sha256(None, tool, "1.0.0", "linux", "amd64")
        assert "not a platform tool" not in str(excinfo.value), (
            f"{tool} is in PLATFORM_TOOLS but _expected_sha256 has no branch for it, "
            f"so it can be downloaded and never verified"
        )

    @pytest.mark.asyncio
    async def test_checkov_reads_the_release_api_digest(self) -> None:
        payload = json.dumps(
            {
                "assets": [
                    {"name": "checkov_linux_arm64.zip", "digest": "sha256:AAAA"},
                    {"name": "checkov_linux_X86_64.zip", "digest": "sha256:BBBB"},
                ]
            }
        )
        with patch(
            "terrapod.services.platform_tools.arequest_with_retry",
            AsyncMock(return_value=_resp(text=payload)),
        ):
            assert await _expected_sha256(None, "checkov", "3.3.8", "linux", "amd64") == "bbbb"

    @pytest.mark.asyncio
    async def test_checkov_refuses_when_the_api_reports_no_digest(self) -> None:
        payload = json.dumps({"assets": [{"name": "checkov_linux_X86_64.zip"}]})
        with patch(
            "terrapod.services.platform_tools.arequest_with_retry",
            AsyncMock(return_value=_resp(text=payload)),
        ):
            with pytest.raises(VerificationError, match="no digest"):
                await _expected_sha256(None, "checkov", "3.3.8", "linux", "amd64")

    @pytest.mark.asyncio
    async def test_unreachable_material_fails_closed(self) -> None:
        """Not being able to check is not the same as checking and passing."""
        with patch(
            "terrapod.services.platform_tools.arequest_with_retry",
            AsyncMock(return_value=_resp(status=503)),
        ):
            with pytest.raises(VerificationError):
                await _expected_sha256(None, "opa", "1.19.0", "linux", "amd64")


class TestVerifyPlatformTool:
    @pytest.mark.asyncio
    async def test_accepts_a_matching_checksum(self) -> None:
        with patch(
            "terrapod.services.platform_tools._expected_sha256",
            AsyncMock(return_value="deadbeef"),
        ):
            await verify_platform_tool(
                None, "opa", "1.19.0", "linux", "amd64", {"sha256": "DEADBEEF"}
            )

    @pytest.mark.asyncio
    async def test_rejects_a_mismatch(self) -> None:
        with patch(
            "terrapod.services.platform_tools._expected_sha256",
            AsyncMock(return_value="deadbeef"),
        ):
            with pytest.raises(VerificationError, match="(checksum|sha256|sha512) mismatch"):
                await verify_platform_tool(
                    None, "opa", "1.19.0", "linux", "amd64", {"sha256": "0badc0de"}
                )

    @pytest.mark.asyncio
    async def test_verify_off_skips_the_check_entirely(self, monkeypatch) -> None:
        """Opt-out exists for mirrors that cannot serve the material — it must
        not merely tolerate a mismatch, it must not fetch at all."""
        from terrapod.config import settings

        monkeypatch.setattr(settings.registry.platform_tools, "verify", "off")
        fetch = AsyncMock()
        with patch("terrapod.services.platform_tools._expected_sha256", fetch):
            await verify_platform_tool(
                None, "opa", "1.19.0", "linux", "amd64", {"sha256": "whatever"}
            )
        fetch.assert_not_awaited()


class TestConfiguredVersion:
    @pytest.mark.parametrize("tool", sorted(PLATFORM_TOOLS))
    def test_every_tool_resolves_to_a_pinned_version(self, tool: str) -> None:
        version = configured_version(tool)
        assert version and len(version.split(".")) >= 2


class TestTheCheckovMirrorAndChecksumApiStayConsistent:
    """The two keys describe one publisher, and the mirror decides.

    They are separate Helm values under separate `{{- if }}` blocks, so an
    operator can move one and leave the other. Terrapod BUILDS the linux Checkov
    binaries, so its digests differ from upstream's by construction -- measured
    on 3.3.21 `checkov_linux_X86_64.zip`, ours `53a06cb7...` against upstream's
    `2c786f82...`. So the documented air-gap pattern (point the mirror at an
    internal proxy of upstream, leave the rest alone) took bytes from one
    publisher and the digest from another, and `verify` defaults to the
    fail-closed `checksum` -- every fetch refused with a mismatch naming neither
    key, and every scan errored.
    """

    @staticmethod
    def _patch(mirror: str, api: str):
        """Override the two keys on the REAL settings object.

        Deliberately not a stand-in tree: `_mirror` reads every tool's URL, and
        on the main line also `registry.binary_cache.*`, so a hand-built
        namespace fails on whichever key that branch happens to need -- for a
        reason that has nothing to do with the property under test, and
        differently per release line.
        """
        from unittest.mock import patch

        from terrapod.services import platform_tools as pt

        cfg = pt.settings.registry.platform_tools
        return (
            patch.object(cfg, "checkov_mirror_url", mirror),
            patch.object(cfg, "checkov_checksum_api_url", api),
        )

    def _call(self, mirror: str, api: str) -> str:
        import contextlib

        from terrapod.services import platform_tools as pt

        with contextlib.ExitStack() as stack:
            for p in self._patch(mirror, api):
                stack.enter_context(p)
            return pt._checkov_checksum_api()

    def test_both_at_their_defaults_uses_the_default_api(self):
        from terrapod.services.platform_tools import (
            _CHECKOV_CHECKSUM_API_DEFAULT,
            _CHECKOV_MIRROR_DEFAULT,
        )

        got = self._call(_CHECKOV_MIRROR_DEFAULT, _CHECKOV_CHECKSUM_API_DEFAULT)
        assert got == _CHECKOV_CHECKSUM_API_DEFAULT.rstrip("/")

    def test_moving_only_the_mirror_derives_the_matching_api(self):
        """The fix. Before, this left the digest pointing at Terrapod's releases
        while the bytes came from somewhere else."""
        from terrapod.services.platform_tools import _CHECKOV_CHECKSUM_API_DEFAULT

        got = self._call(
            "https://github.com/acme/mirror/releases/download", _CHECKOV_CHECKSUM_API_DEFAULT
        )
        assert got == "https://api.github.com/repos/acme/mirror/releases/tags"

    def test_an_explicit_api_is_never_overridden(self):
        """An operator who set both deliberately knows where their digests live;
        deriving over the top of that would be the worse failure."""
        got = self._call(
            "https://artifactory.internal/gh/checkov", "https://artifactory.internal/api/tags"
        )
        assert got == "https://artifactory.internal/api/tags"

    def test_a_mirror_we_cannot_derive_from_is_refused_BY_NAME(self):
        """The point of the whole change: the old behaviour was a digest
        mismatch naming neither key, which an operator cannot act on."""
        import pytest

        from terrapod.services.artifact_verification import VerificationError
        from terrapod.services.platform_tools import _CHECKOV_CHECKSUM_API_DEFAULT

        with pytest.raises(VerificationError) as exc:
            self._call("https://artifactory.internal/gh/checkov", _CHECKOV_CHECKSUM_API_DEFAULT)
        msg = str(exc.value)
        assert "checkov_mirror_url" in msg
        assert "checkov_checksum_api_url" in msg
        # And it says what to do, both ways.
        assert "verify=off" in msg

    def test_a_trailing_slash_on_either_does_not_change_the_answer(self):
        """`_mirror` rstrips, so a trailing slash is a legitimate way to write
        the value and must not read as 'customised'."""
        from terrapod.services.platform_tools import (
            _CHECKOV_CHECKSUM_API_DEFAULT,
            _CHECKOV_MIRROR_DEFAULT,
        )

        got = self._call(_CHECKOV_MIRROR_DEFAULT + "/", _CHECKOV_CHECKSUM_API_DEFAULT + "/")
        assert got == _CHECKOV_CHECKSUM_API_DEFAULT.rstrip("/")

    async def test_the_fetch_path_uses_the_derived_api(self):
        """Drives `_expected_sha256` itself. Reverting its call site to read the
        config key directly leaves every other test in this class green, because
        they all call the helper by hand -- so this is the one that fails."""
        from unittest.mock import AsyncMock, patch

        from terrapod.services import platform_tools as pt
        from terrapod.services.platform_tools import _CHECKOV_CHECKSUM_API_DEFAULT

        seen: list[str] = []

        class _Resp:
            status_code = 200
            text = '{"assets":[{"name":"checkov_linux_X86_64.zip","digest":"sha256:ABCDEF"}]}'

        async def _req(_client, _method, url, *a, **k):
            seen.append(url)
            return _Resp()

        import contextlib

        with contextlib.ExitStack() as stack:
            for p in self._patch(
                "https://github.com/acme/mirror/releases/download", _CHECKOV_CHECKSUM_API_DEFAULT
            ):
                stack.enter_context(p)
            stack.enter_context(
                patch.object(pt, "arequest_with_retry", AsyncMock(side_effect=_req))
            )
            got = await pt._expected_sha256(None, "checkov", "3.3.26", "linux", "amd64")

        assert got == "abcdef"
        assert seen, "the fetch path made no request"
        assert seen[0].startswith("https://api.github.com/repos/acme/mirror/releases/tags"), (
            f"the fetch path used {seen[0]!r} -- it must follow the mirror, not the "
            f"default checksum API"
        )
