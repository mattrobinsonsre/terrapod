"""Pulumi plugin filenames, and whose plugin gets verified (#1483, #2589).

The protocol is one request for one well-known filename, so the filename *is*
the request — and it is client input that reaches an upstream URL and a storage
key. Parsing it strictly is the whole of the request-forgery surface here, which
is why most of these tests are about what gets refused.

The shape was captured from a real `pulumi` CLI (`scripts/pulumi-capture.py`),
whose own template is literally `pulumi-%s-%s-v%s-%s-%s.tar.gz`.

The classes from `TestWhichPLUGINCarriesADigestResolver` down are the second
subject: `get.pulumi.com` publishes no digest beside a tarball, so a
Pulumi-published plugin is admitted and recorded `none-published`, while
**Terrapod's own bridged plugin is required to carry one** because we are its
publisher. One predicate decides both which base a tarball comes from and
whether its digest is required, so the tests assert the BICONDITIONAL rather
than the first-party case alone — a branch that captured every plugin satisfies
"ours is verified" perfectly well and would refuse `random` for every install.

The manifest fixture is the real one from the published v2.0.0-beta.2 release,
trimmed to three platforms, rather than a shape written from the GoReleaser
config.
"""

from __future__ import annotations

import httpx
import pytest

from terrapod import http_retry
from terrapod.config import settings
from terrapod.services.package_cache import pulumi_plugins
from terrapod.services.package_cache.substrate import UpstreamError


@pytest.fixture
def zero_retry_delay(monkeypatch) -> None:
    """Retries are bounded but the delay is not worth waiting out in a test."""
    monkeypatch.setattr(http_retry, "_backoff_seconds", lambda *_: 0.0)


@pytest.fixture(autouse=True)
def _upstream():
    before = settings.registry.package_cache.pulumi.upstream
    settings.registry.package_cache.pulumi.upstream = "https://get.pulumi.com/releases/plugins"
    yield
    settings.registry.package_cache.pulumi.upstream = before


class TestParsingAPluginFilename:
    def test_a_resource_plugin(self) -> None:
        parts = pulumi_plugins.parse_filename("pulumi-resource-random-v4.16.3-linux-amd64.tar.gz")
        assert parts == {
            "kind": "resource",
            "name": "random",
            "version": "4.16.3",
            "os": "linux",
            "arch": "amd64",
        }

    def test_a_language_plugin_parses_the_same_way(self) -> None:
        """Language plugins share the shape, so they work by construction.

        Asserted rather than assumed, because "should work by construction" is
        how an untested path gets shipped.
        """
        parts = pulumi_plugins.parse_filename("pulumi-language-python-v3.145.0-darwin-arm64.tar.gz")
        assert parts is not None
        assert parts["kind"] == "language"
        assert parts["name"] == "python"

    def test_a_hyphenated_plugin_name(self) -> None:
        """`aws-native` is a real plugin, and a greedy name would swallow the
        version."""
        parts = pulumi_plugins.parse_filename(
            "pulumi-resource-aws-native-v0.98.0-linux-arm64.tar.gz"
        )
        assert parts is not None
        assert parts["name"] == "aws-native"
        assert parts["version"] == "0.98.0"

    @pytest.mark.parametrize(
        "version",
        ["4.16.3", "1.0.0-alpha.1", "2.0.0+build.5", "0.1.0-rc1"],
    )
    def test_prerelease_and_build_versions(self, version: str) -> None:
        parts = pulumi_plugins.parse_filename(
            f"pulumi-resource-random-v{version}-linux-amd64.tar.gz"
        )
        assert parts is not None
        assert parts["version"] == version


class TestWhatIsRefused:
    """A filename that is not a plugin names nothing this proxy has.

    Each of these would otherwise be interpolated into an upstream URL or a
    storage key, so the pattern is a boundary rather than a tidiness check.
    """

    @pytest.mark.parametrize(
        "filename",
        [
            "../../../etc/passwd",
            "pulumi-resource-../-v1.0.0-linux-amd64.tar.gz",
            "pulumi-resource-random-v4.16.3-linux-amd64.tar.gz/../evil",
            "http://evil.example.com/x.tar.gz",
            "pulumi-resource-random-v4.16.3-linux-amd64.zip",
            "random-v4.16.3-linux-amd64.tar.gz",
            "pulumi-resource-random-4.16.3-linux-amd64.tar.gz",
            "pulumi-resource-random-vNOTAVERSION-linux-amd64.tar.gz",
            "pulumi-resource-random-v4.16.3-linux.tar.gz",
            "",
            "pulumi-RESOURCE-random-v4.16.3-linux-amd64.tar.gz",
        ],
    )
    def test_it_is_not_a_plugin_filename(self, filename: str) -> None:
        assert pulumi_plugins.parse_filename(filename) is None


class TestTheUpstreamURLIsRebuilt:
    """Never the client's string, always one composed from the parsed parts.

    A filename that satisfies the pattern is still someone else's input;
    rebuilding means the request cannot carry anything the pattern did not
    explicitly allow.
    """

    def test_it_points_at_the_configured_upstream(self) -> None:
        name = "pulumi-resource-random-v4.16.3-linux-amd64.tar.gz"
        artifact = pulumi_plugins.artifact_for(name, pulumi_plugins.parse_filename(name))
        assert artifact.upstream_url == (f"https://get.pulumi.com/releases/plugins/{name}")

    def test_an_internal_mirror_is_honoured(self) -> None:
        settings.registry.package_cache.pulumi.upstream = "https://mirror.internal/plugins/"
        name = "pulumi-resource-random-v4.16.3-linux-amd64.tar.gz"
        artifact = pulumi_plugins.artifact_for(name, pulumi_plugins.parse_filename(name))
        assert artifact.upstream_url == f"https://mirror.internal/plugins/{name}"

    def test_platforms_of_one_plugin_share_a_cache_name(self) -> None:
        """So `cached_filenames` for a plugin lists the platforms held, the way
        the other ecosystems key theirs."""
        linux = "pulumi-resource-random-v4.16.3-linux-amd64.tar.gz"
        darwin = "pulumi-resource-random-v4.16.3-darwin-arm64.tar.gz"
        a = pulumi_plugins.artifact_for(linux, pulumi_plugins.parse_filename(linux))
        b = pulumi_plugins.artifact_for(darwin, pulumi_plugins.parse_filename(darwin))
        assert a.name == b.name == "pulumi-resource-random"
        assert a.filename != b.filename

    def test_no_digest_is_claimed(self) -> None:
        """Upstream publishes none alongside the tarball.

        Recording an empty digest is the honest answer; inventing one would
        imply an integrity check this proxy does not perform.
        """
        name = "pulumi-resource-random-v4.16.3-linux-amd64.tar.gz"
        artifact = pulumi_plugins.artifact_for(name, pulumi_plugins.parse_filename(name))
        assert artifact.digest == ""


class TestTerrapodsOwnPluginResolvesSomewhereElse:
    """Terrapod publishes a Pulumi plugin of its own (#1987), and it is not
    published where Pulumi's are.

    The proxy has ONE `upstream`, deliberately -- the client names a package and
    Terrapod decides where packages come from, so that single value is the whole
    request-forgery surface. Our own plugin therefore cannot arrive through it,
    and the alternatives are worse than a second base: overriding `upstream`
    wholesale would send every Pulumi-published plugin to a host that does not
    have it, and pointing the CLI straight at a release URL would make an
    air-gapped install reach upstream, which is the thing the proxy exists to
    prevent.
    """

    @pytest.fixture(autouse=True)
    def _first_party(self):
        before = settings.registry.package_cache.pulumi.first_party_url
        settings.registry.package_cache.pulumi.first_party_url = (
            "https://github.com/mattrobinsonsre/terrapod/releases/download"
        )
        yield
        settings.registry.package_cache.pulumi.first_party_url = before

    def test_it_resolves_against_the_first_party_base(self) -> None:
        name = "pulumi-resource-terrapod-v1.2.3-linux-amd64.tar.gz"
        artifact = pulumi_plugins.artifact_for(name, pulumi_plugins.parse_filename(name))
        assert artifact.upstream_url == (
            "https://github.com/mattrobinsonsre/terrapod/releases/download"
            "/v1.2.3/pulumi-resource-terrapod-v1.2.3-linux-amd64.tar.gz"
        )

    def test_the_version_is_a_PATH_SEGMENT_not_part_of_the_filename(self) -> None:
        """A GitHub release asset lives under its tag, so the base cannot be
        flat the way `upstream` is. Same shape as the `pulumi_mirror_url` the
        binary cache already assembles for the Pulumi CLI itself."""
        name = "pulumi-resource-terrapod-v9.9.9-darwin-arm64.tar.gz"
        url = pulumi_plugins.artifact_for(name, pulumi_plugins.parse_filename(name)).upstream_url
        assert "/v9.9.9/" in url, url
        assert url.endswith("/pulumi-resource-terrapod-v9.9.9-darwin-arm64.tar.gz")

    def test_EVERY_OTHER_PLUGIN_still_goes_to_the_pulumi_upstream(self) -> None:
        """The half that makes the first one mean something.

        Asserting only that our plugin reaches the release base would pass just
        as well if the branch had captured every plugin -- which is the damaging
        direction, because it would send `random` and `aws` to a host that has
        never published them.
        """
        for plugin in ("random", "aws", "aws-native", "terrapod-something-else"):
            name = f"pulumi-resource-{plugin}-v1.0.0-linux-amd64.tar.gz"
            url = pulumi_plugins.artifact_for(
                name, pulumi_plugins.parse_filename(name)
            ).upstream_url
            assert url.startswith("https://get.pulumi.com/releases/plugins/"), (plugin, url)

    def test_a_NON_RESOURCE_plugin_of_the_same_name_is_not_diverted(self) -> None:
        """`kind` is matched as well as the name, so a hypothetical
        `pulumi-language-terrapod` does not resolve against a release base that
        has no such asset -- which would 404 while naming our own release, the
        most confusing possible failure."""
        name = "pulumi-language-terrapod-v1.2.3-linux-amd64.tar.gz"
        url = pulumi_plugins.artifact_for(name, pulumi_plugins.parse_filename(name)).upstream_url
        assert url.startswith("https://get.pulumi.com/releases/plugins/"), url

    def test_an_internal_mirror_is_honoured_for_it_too(self) -> None:
        """A restricted-network install mirrors our release like any other
        artifact, so this base is operator-settable for the same reason
        `upstream` is."""
        settings.registry.package_cache.pulumi.first_party_url = "https://mirror.internal/tp"
        name = "pulumi-resource-terrapod-v1.2.3-linux-amd64.tar.gz"
        url = pulumi_plugins.artifact_for(name, pulumi_plugins.parse_filename(name)).upstream_url
        assert (
            url
            == "https://mirror.internal/tp/v1.2.3/pulumi-resource-terrapod-v1.2.3-linux-amd64.tar.gz"
        )

    def test_the_rebuilt_filename_is_still_the_parsed_one(self) -> None:
        """The module's own invariant, asserted on the new branch as well: the
        URL is rebuilt from PARSED parts, so a filename that survived the
        pattern cannot smuggle a path into the request. A traversal attempt
        should not parse at all, and if it ever did it must not reach the
        release base verbatim."""
        assert (
            pulumi_plugins.parse_filename("pulumi-resource-terrapod-v1.2.3-../../etc-passwd.tar.gz")
            is None
        )


RELEASE_BASE = "https://github.com/mattrobinsonsre/terrapod/releases/download"

#: The real manifest from the v2.0.0-beta.2 release, trimmed to three platforms.
#: `<hex>  <filename>` with two spaces and no `*`, which is what GoReleaser
#: writes -- taken from the published asset rather than written from the docs,
#: because the shape is the whole of what `resolve_first_party_digest` parses.
MANIFEST = (
    "51c21167c9d35fb6b0bea7d68f6163b24aadd5ebe9922f5e5f2a5f7b81d2b9b9  "
    "pulumi-resource-terrapod-v2.0.0-beta.2-darwin-amd64.tar.gz\n"
    "9bed11428e6678d36aa319a7a6e955ba05190fc3f8050da14931bd1aeae8c9f1  "
    "pulumi-resource-terrapod-v2.0.0-beta.2-linux-amd64.tar.gz\n"
    "806a94dc0a395b2c533eaf9b7539497475167f8aa2acc029107bdc09172f8c40  "
    "pulumi-resource-terrapod-v2.0.0-beta.2-linux-arm64.tar.gz\n"
)
LINUX_AMD64 = "pulumi-resource-terrapod-v2.0.0-beta.2-linux-amd64.tar.gz"
LINUX_AMD64_SHA = "9bed11428e6678d36aa319a7a6e955ba05190fc3f8050da14931bd1aeae8c9f1"


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _serving(body: str, *, status: int = 200, seen: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        return httpx.Response(status, content=body.encode())

    return _client(handler)


class TestWhichPLUGINCarriesADigestResolver:
    """The predicate decides two things at once, and only one of them is visible.

    `is_first_party` picks both the base the tarball comes from AND whether a
    digest is required. A second copy of that decision would eventually disagree,
    and the damaging direction is a resolver attached to a Pulumi-published
    plugin: it would look for a manifest on OUR release for an asset never
    published there, and refuse `random` and `aws` for every install.

    So these assert the BICONDITIONAL rather than the first-party case alone -- a
    branch that captured every plugin would satisfy "ours has a resolver"
    perfectly well.
    """

    @pytest.fixture(autouse=True)
    def _first_party(self):
        before = settings.registry.package_cache.pulumi.first_party_url
        settings.registry.package_cache.pulumi.first_party_url = RELEASE_BASE
        yield
        settings.registry.package_cache.pulumi.first_party_url = before

    @pytest.mark.parametrize("kind", ["resource", "language", "converter"])
    @pytest.mark.parametrize("name", ["terrapod", "random", "aws-native", "terrapod-something"])
    def test_a_resolver_is_attached_IFF_the_tarball_comes_from_our_release(
        self, kind: str, name: str
    ) -> None:
        """Derived over the cross product rather than listed, so a new plugin
        name or kind is covered without anyone adding a case."""
        filename = f"pulumi-{kind}-{name}-v1.2.3-linux-amd64.tar.gz"
        parts = pulumi_plugins.parse_filename(filename)
        assert parts is not None, filename
        artifact = pulumi_plugins.artifact_for(filename, parts)

        from_our_release = artifact.upstream_url.startswith(RELEASE_BASE)
        assert from_our_release == (artifact.digest_resolver is not None), (
            f"{filename}: fetched from {artifact.upstream_url} but "
            f"digest_resolver={artifact.digest_resolver!r}"
        )

    def test_our_own_plugin_does_carry_one(self) -> None:
        """The positive half, stated outright. Without it the biconditional above
        is satisfied by a branch that attaches a resolver to nothing at all."""
        filename = "pulumi-resource-terrapod-v1.2.3-linux-amd64.tar.gz"
        artifact = pulumi_plugins.artifact_for(filename, pulumi_plugins.parse_filename(filename))
        assert artifact.digest_resolver is not None

    def test_a_pulumi_published_plugin_carries_none(self) -> None:
        filename = "pulumi-resource-random-v4.16.3-linux-amd64.tar.gz"
        artifact = pulumi_plugins.artifact_for(filename, pulumi_plugins.parse_filename(filename))
        assert artifact.digest_resolver is None

    def test_a_MIRRORED_first_party_plugin_carries_none(self) -> None:
        """A warm `base` override replaces the whole upstream, flat, with no
        version path segment -- so the manifest is not at a place we can derive,
        and requiring one would break warming from a mirror that holds the
        tarballs. Unchanged behaviour: admitted and recorded `none-published`.
        """
        filename = "pulumi-resource-terrapod-v1.2.3-linux-amd64.tar.gz"
        artifact = pulumi_plugins.artifact_for(
            filename, pulumi_plugins.parse_filename(filename), base="https://mirror.internal/p"
        )
        assert artifact.upstream_url == f"https://mirror.internal/p/{filename}"
        assert artifact.digest_resolver is None

    def test_NO_plugin_carries_a_digest_on_the_artifact_itself(self) -> None:
        """An Artifact is constructed on EVERY request including a cache hit, so
        a digest resolved during construction would put an upstream round-trip on
        the hit path and break `cache_only` outright."""
        for filename in (
            "pulumi-resource-terrapod-v1.2.3-linux-amd64.tar.gz",
            "pulumi-resource-random-v4.16.3-linux-amd64.tar.gz",
        ):
            artifact = pulumi_plugins.artifact_for(
                filename, pulumi_plugins.parse_filename(filename)
            )
            assert artifact.digest == "", filename


class TestTheFirstPartyChecksumsManifestURL:
    @pytest.fixture(autouse=True)
    def _first_party(self):
        before = settings.registry.package_cache.pulumi.first_party_url
        settings.registry.package_cache.pulumi.first_party_url = RELEASE_BASE
        yield
        settings.registry.package_cache.pulumi.first_party_url = before

    def test_the_version_is_spelled_BOTH_ways_in_one_URL(self) -> None:
        """`v2.0.0-beta.2` as the release's path segment, `2.0.0-beta.2` in the
        manifest's own name, because GoReleaser's `{{ .Version }}` strips the `v`.

        Asserted against the URL the published release actually serves, not
        against the template: both spellings in one string is exactly the shape a
        reader `v`-prefixes twice and 404s on.
        """
        assert pulumi_plugins.first_party_checksums_url("2.0.0-beta.2") == (
            f"{RELEASE_BASE}/v2.0.0-beta.2/pulumi-resource-terrapod_2.0.0-beta.2_SHA256SUMS"
        )

    def test_it_sits_beside_the_tarball_it_vouches_for(self) -> None:
        """Same release, same directory. A manifest fetched from anywhere else
        would be vouching for an asset it has no relationship to."""
        filename = "pulumi-resource-terrapod-v2.0.0-beta.2-linux-amd64.tar.gz"
        artifact = pulumi_plugins.artifact_for(filename, pulumi_plugins.parse_filename(filename))
        manifest = pulumi_plugins.first_party_checksums_url("2.0.0-beta.2")
        assert artifact.upstream_url.rsplit("/", 1)[0] == manifest.rsplit("/", 1)[0]

    def test_an_internal_mirror_must_carry_the_manifest_too(self) -> None:
        """`first_party_url` moves both, which is the documented consequence: a
        restricted-network mirror of our release holding only the tarballs now
        refuses them rather than caching them unverified."""
        settings.registry.package_cache.pulumi.first_party_url = "https://mirror.internal/tp"
        assert pulumi_plugins.first_party_checksums_url("1.2.3") == (
            "https://mirror.internal/tp/v1.2.3/pulumi-resource-terrapod_1.2.3_SHA256SUMS"
        )


class TestResolvingTheFirstPartyDigest:
    """Best effort, and the split is the point.

    An ABSENT manifest returns `""`, which routes the tarball into the
    `none-published` branch: admitted, recorded honestly as unverified, and logged.
    A manifest that IS there and makes a claim we cannot read, or that disagrees
    with the bytes, REFUSES.

    These tests asserted a refusal in every case until 2026-11-16, on the reasoning
    that we are the publisher so an absence is a statement about our own release.
    Sound as far as it goes, and it broke a supported configuration: an operator who
    has pointed `first_party_url` at an internal mirror carrying only the tarballs
    would have had those plugins refused on upgrade, under config they already had,
    with no warning. The integrity check that matters -- do these bytes match what
    was published -- is unaffected by admitting an absence.
    """

    @pytest.fixture(autouse=True)
    def _first_party(self):
        before = settings.registry.package_cache.pulumi.first_party_url
        settings.registry.package_cache.pulumi.first_party_url = RELEASE_BASE
        yield
        settings.registry.package_cache.pulumi.first_party_url = before

    async def test_it_returns_the_line_for_THIS_asset(self) -> None:
        seen: list[str] = []
        async with _serving(MANIFEST, seen=seen) as client:
            got = await pulumi_plugins.resolve_first_party_digest(
                "2.0.0-beta.2", LINUX_AMD64, client
            )
        assert got == f"sha256:{LINUX_AMD64_SHA}"
        assert seen == [pulumi_plugins.first_party_checksums_url("2.0.0-beta.2")]

    async def test_a_sibling_platforms_line_is_not_taken(self) -> None:
        """Six platforms share one manifest, so matching the wrong line would
        refuse every plugin with a digest from another architecture -- which reads
        as a corrupted download rather than as a parser picking the first row."""
        async with _serving(MANIFEST) as client:
            arm = await pulumi_plugins.resolve_first_party_digest(
                "2.0.0-beta.2",
                "pulumi-resource-terrapod-v2.0.0-beta.2-linux-arm64.tar.gz",
                client,
            )
        assert arm == "sha256:806a94dc0a395b2c533eaf9b7539497475167f8aa2acc029107bdc09172f8c40"

    async def test_a_binary_mode_star_is_stripped(self) -> None:
        """GoReleaser does not write one, but `shasum -b` does, and the
        platform-tool manifests already strip it. Cheap to tolerate, and a
        manifest regenerated by hand is exactly when it would appear."""
        body = f"{LINUX_AMD64_SHA}  *{LINUX_AMD64}\n"
        async with _serving(body) as client:
            got = await pulumi_plugins.resolve_first_party_digest(
                "2.0.0-beta.2", LINUX_AMD64, client
            )
        assert got == f"sha256:{LINUX_AMD64_SHA}"

    async def test_an_asset_absent_from_the_manifest_is_ADMITTED_unverified(self) -> None:
        """The manifest is there and does not list this platform -- a partial release,
        or a mirror that carries a subset. Nothing to verify against, so nothing is
        claimed: `""` routes it to `none-published` rather than refusing it."""
        async with _serving(MANIFEST) as client:
            got = await pulumi_plugins.resolve_first_party_digest(
                "2.0.0-beta.2",
                "pulumi-resource-terrapod-v2.0.0-beta.2-windows-arm64.tar.gz",
                client,
            )
        assert got == ""

    async def test_an_absent_manifest_is_ADMITTED_unverified(self) -> None:
        """A 404 is an internal mirror holding only tarballs as well as a typo, and
        the mirror is a supported, documented configuration. Refusing would break it
        on upgrade; the row records `none-published` and the absence is logged."""
        async with _serving("Not Found", status=404) as client:
            got = await pulumi_plugins.resolve_first_party_digest(
                "2.0.0-beta.2", LINUX_AMD64, client
            )
        assert got == ""

    async def test_an_empty_manifest_is_ADMITTED_unverified(self) -> None:
        async with _serving("") as client:
            got = await pulumi_plugins.resolve_first_party_digest(
                "2.0.0-beta.2", LINUX_AMD64, client
            )
        assert got == ""

    async def test_a_line_that_is_not_a_sha256_is_REFUSED(self) -> None:
        """Named here rather than left to `substrate._parse_digest`, which would
        answer "digest is not hex" and name no manifest -- so an operator would
        go looking at the artifact instead of at the release that published it."""
        async with _serving(f"not-a-digest  {LINUX_AMD64}\n") as client:
            with pytest.raises(UpstreamError, match="which is not a sha256"):
                await pulumi_plugins.resolve_first_party_digest("2.0.0-beta.2", LINUX_AMD64, client)

    async def test_a_TRUNCATED_sha256_is_REFUSED(self) -> None:
        """Hex, and a prefix of the real digest. A length-blind parser would hand
        it to `_parse_digest`, which refuses it -- so this pins WHERE the refusal
        happens, not whether one happens at all."""
        async with _serving(f"{LINUX_AMD64_SHA[:32]}  {LINUX_AMD64}\n") as client:
            with pytest.raises(UpstreamError, match="which is not a sha256"):
                await pulumi_plugins.resolve_first_party_digest("2.0.0-beta.2", LINUX_AMD64, client)

    async def test_a_transport_failure_is_ADMITTED_unverified(self, zero_retry_delay) -> None:
        """An unreachable digest source is indistinguishable from an absent one from
        here, and both get the same answer: admit, record `none-published`, log it.

        This does mean verification depends on the weather, which is a real cost and
        the reason the absence is logged at WARNING rather than swallowed. The
        alternative costs more: a plugin pull that fails because a manifest host
        blipped, on a path where the bytes themselves were fine."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        async with _client(handler) as client:
            got = await pulumi_plugins.resolve_first_party_digest(
                "2.0.0-beta.2", LINUX_AMD64, client
            )
        assert got == ""

    async def test_a_persistent_5xx_is_RETRIED_then_admitted_unverified(
        self, zero_retry_delay
    ) -> None:
        """The retry still matters: giving up on the first 5xx would downgrade a
        plugin to unverified over one blip, where a retry usually recovers the
        manifest and verifies it properly."""
        seen: list[str] = []
        async with _serving("boom", status=503, seen=seen) as client:
            got = await pulumi_plugins.resolve_first_party_digest(
                "2.0.0-beta.2", LINUX_AMD64, client
            )
        assert got == ""
        assert len(seen) > 1, "a 5xx should have been retried before giving up"

    async def test_it_NEVER_INVENTS_a_digest(self) -> None:
        """The property every test above is an instance of, asserted as itself, and
        the inverse of what this test asserted before 2026-11-16.

        The resolver may return a digest it read, or `""` to say it has none. What it
        must never do is return something for an asset the manifest does not cover:
        that would verify one artifact's bytes against another's hash, which refuses
        a perfectly good download and reads as corruption.
        """
        bodies = [MANIFEST, "", "garbage"]
        for body in bodies:
            async with _serving(body) as client:
                got = await pulumi_plugins.resolve_first_party_digest(
                    "2.0.0-beta.2", "pulumi-resource-terrapod-v9.9.9-aix-ppc.tar.gz", client
                )
            assert got == "", f"invented {got!r} for an asset absent from {body!r}"

        # And a manifest that DOES name the asset against an unreadable digest is a
        # refusal, not an absence -- it is making a claim we cannot parse.
        async with _serving(f"zz  {LINUX_AMD64}\n") as client:
            with pytest.raises(UpstreamError, match="which is not a sha256"):
                await pulumi_plugins.resolve_first_party_digest("2.0.0-beta.2", LINUX_AMD64, client)
