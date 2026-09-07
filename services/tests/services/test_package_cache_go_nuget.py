"""Go module and NuGet path handling (#1484).

Both protocols address everything by path, so the path *is* the request — and it
is client input that reaches an upstream URL and a storage key. Most of these
tests are therefore about what gets refused.

The shapes were captured from real clients (`scripts/goproxy-capture.py`,
`scripts/nuget-capture.py`) rather than read from documentation, which is how
the two properties that would have broken common use were found: Go escapes
uppercase, and NuGet's service index advertises absolute URLs the client follows
verbatim.
"""

from __future__ import annotations

import json

import pytest

from terrapod.config import settings
from terrapod.services.package_cache import goproxy, nuget


@pytest.fixture(autouse=True)
def _upstreams():
    go_before = settings.registry.package_cache.go.upstream
    nu_before = settings.registry.package_cache.nuget.upstream
    settings.registry.package_cache.go.upstream = "https://proxy.golang.org"
    settings.registry.package_cache.nuget.upstream = "https://api.nuget.org/v3-flatcontainer"
    yield
    settings.registry.package_cache.go.upstream = go_before
    settings.registry.package_cache.nuget.upstream = nu_before


# ── Go ──────────────────────────────────────────────────────────────────────


class TestGoModulePaths:
    def test_the_escaped_form_is_accepted_and_passed_through(self) -> None:
        """`github.com/BurntSushi/toml` arrives as `!burnt!sushi`.

        Upstream expects the same escaped form, so it is forwarded verbatim.
        Decoding and re-encoding would be two chances to get a very common
        module wrong, and this test exists because that module is common enough
        that getting it wrong would be noticed immediately — by users.
        """
        module = "github.com/!burnt!sushi/toml"
        assert goproxy.valid_module(module)
        artifact = goproxy.artifact_for(module, "v1.3.2", ".zip")
        assert artifact.upstream_url == (
            "https://proxy.golang.org/github.com/!burnt!sushi/toml/@v/v1.3.2.zip"
        )

    @pytest.mark.parametrize(
        "module",
        [
            "example.com/../etc",
            "..",
            "example.com/a/../../b",
            "example.com/a b",
            "http://example.com/a",
            "example.com/a?x=1",
            "",
        ],
    )
    def test_a_path_that_could_climb_is_refused(self, module: str) -> None:
        assert not goproxy.valid_module(module)

    @pytest.mark.parametrize(
        "module",
        [
            "example.com/tinymod",
            "github.com/!burnt!sushi/toml",
            "gopkg.in/yaml.v3",
            "k8s.io/api",
            "github.com/a-b/c_d",
        ],
    )
    def test_real_module_paths_are_accepted(self, module: str) -> None:
        assert goproxy.valid_module(module)

    @pytest.mark.parametrize(
        "version",
        ["v1.0.0", "v0.0.0-20240101120000-abcdef123456", "v2.1.0-rc1+meta"],
    )
    def test_real_versions_are_accepted(self, version: str) -> None:
        assert goproxy.valid_version(version)

    @pytest.mark.parametrize("version", ["1.0.0", "../v1.0.0", "v1 0", ""])
    def test_a_version_that_is_not_one_is_refused(self, version: str) -> None:
        """`1.0.0` without the `v` included: the proxy protocol always carries
        it, so its absence means something other than a version arrived."""
        assert not goproxy.valid_version(version)


class TestGoArtifacts:
    @pytest.mark.parametrize(
        ("suffix", "content_type"),
        [
            (".info", "application/json"),
            (".mod", "text/plain; charset=UTF-8"),
            (".zip", "application/zip"),
        ],
    )
    def test_each_immutable_file(self, suffix: str, content_type: str) -> None:
        artifact = goproxy.artifact_for("example.com/m", "v1.0.0", suffix)
        assert artifact.filename == f"v1.0.0{suffix}"
        assert artifact.content_type == content_type

    def test_versions_of_one_module_share_a_cache_name(self) -> None:
        a = goproxy.artifact_for("example.com/m", "v1.0.0", ".zip")
        b = goproxy.artifact_for("example.com/m", "v2.0.0", ".zip")
        assert a.name == b.name == "example.com/m"
        assert a.filename != b.filename

    def test_no_digest_is_claimed(self) -> None:
        """The proxy protocol publishes none; integrity comes from the client's
        own checksum database, which it consults independently of us."""
        assert goproxy.artifact_for("example.com/m", "v1.0.0", ".zip").digest == ""


# ── NuGet ───────────────────────────────────────────────────────────────────


class TestTheServiceIndex:
    """The finding this feature turns on.

    The client follows the advertised `@id` verbatim. The capture showed
    `dotnet restore` following one to a host it could not reach and giving up —
    so the index must carry the caller's own base *and* this proxy's path
    prefix, and must be built per request rather than stored.
    """

    def test_the_advertised_url_carries_the_whole_base(self) -> None:
        base = "https://terrapod.example.com/api/terrapod/v1/package-cache/nuget"
        index = nuget.service_index(base)
        advertised = index["resources"][0]["@id"]
        assert advertised == f"{base}/flat/"

    def test_the_path_prefix_survives(self) -> None:
        """A hardcoded root would drop it, and restore would follow nowhere."""
        base = "https://host/deep/prefix/nuget"
        assert "/deep/prefix/nuget/flat/" in nuget.service_index(base)["resources"][0]["@id"]

    def test_it_advertises_only_what_we_serve(self) -> None:
        """Advertising a resource we do not serve sends the client somewhere
        that 404s at a point it has already committed."""
        index = nuget.service_index("https://host/nuget")
        assert [r["@type"] for r in index["resources"]] == ["PackageBaseAddress/3.0.0"]

    def test_it_is_valid_json_with_the_protocol_version(self) -> None:
        index = nuget.service_index("https://host/nuget")
        assert json.loads(json.dumps(index))["version"] == "3.0.0"


class TestNuGetPaths:
    def test_ids_are_lowercased_so_one_package_is_one_entry(self) -> None:
        """The client lowercases before asking; doing the same here means
        `Newtonsoft.Json` and `newtonsoft.json` are not two copies of identical
        bytes."""
        upper = nuget.package_artifact("Newtonsoft.Json", "13.0.3")
        lower = nuget.package_artifact("newtonsoft.json", "13.0.3")
        assert upper.name == lower.name == "newtonsoft.json"
        assert upper.filename == lower.filename == "newtonsoft.json.13.0.3.nupkg"

    def test_the_upstream_url_uses_the_configured_base(self) -> None:
        artifact = nuget.package_artifact("Newtonsoft.Json", "13.0.3")
        assert artifact.upstream_url == (
            "https://api.nuget.org/v3-flatcontainer/newtonsoft.json/13.0.3/"
            "newtonsoft.json.13.0.3.nupkg"
        )

    def test_an_internal_mirror_is_honoured(self) -> None:
        settings.registry.package_cache.nuget.upstream = "https://mirror.internal/flat/"
        artifact = nuget.package_artifact("A", "1.0.0")
        assert artifact.upstream_url.startswith("https://mirror.internal/flat/a/1.0.0/")

    @pytest.mark.parametrize("bad", ["../etc", "a/b", "a b", "", "..", "a?b"])
    def test_an_unusable_id_is_refused(self, bad: str) -> None:
        assert not nuget.valid_id(bad)

    @pytest.mark.parametrize("good", ["Newtonsoft.Json", "System.Text.Json", "a-b_c", "A1"])
    def test_real_ids_are_accepted(self, good: str) -> None:
        assert nuget.valid_id(good)

    @pytest.mark.parametrize("good", ["1.0.0", "13.0.3", "2.1.0-beta.1", "1.0.0+meta", "1.0.0.1"])
    def test_real_versions_are_accepted(self, good: str) -> None:
        assert nuget.valid_version(good)

    @pytest.mark.parametrize("bad", ["../1.0.0", "v1.0.0", "", "1.0.0/x"])
    def test_an_unusable_version_is_refused(self, bad: str) -> None:
        assert not nuget.valid_version(bad)

    def test_no_digest_is_claimed(self) -> None:
        assert nuget.package_artifact("A", "1.0.0").digest == ""
