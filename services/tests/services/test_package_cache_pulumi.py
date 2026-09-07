"""Pulumi plugin filenames (#1483).

The protocol is one request for one well-known filename, so the filename *is*
the request — and it is client input that reaches an upstream URL and a storage
key. Parsing it strictly is the whole of the request-forgery surface here, which
is why most of these tests are about what gets refused.

The shape was captured from a real `pulumi` CLI (`scripts/pulumi-capture.py`),
whose own template is literally `pulumi-%s-%s-v%s-%s-%s.tar.gz`.
"""

from __future__ import annotations

import pytest

from terrapod.config import settings
from terrapod.services.package_cache import pulumi_plugins


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
