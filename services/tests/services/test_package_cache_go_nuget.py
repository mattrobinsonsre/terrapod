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

    @pytest.mark.parametrize("suffix", [".info", ".mod", ".zip"])
    def test_no_digest_is_claimed_and_none_is_resolvable(self, suffix: str) -> None:
        """The proxy protocol publishes none (`.ziphash` and `.zip.sha256` are
        both 404), and unlike NuGet there is no second upstream to fetch one
        from that we are willing to reach -- so there is no resolver either and
        the row records `none-published`.

        All three files, because a digest claimed for one of them and not the
        others would be the shape nobody checks: `.zip` is the one anybody looks
        at, and `.mod` and `.info` go through the identical path.

        The ASSERTION ON THE RESOLVER is the half that matters going forward. A
        future checksum-database client would be wired as a `digest_resolver`,
        and this is the test that would have to be changed to add one -- which is
        the intended friction, because the decision not to is recorded rather
        than accidental. See the sibling class for why.
        """
        artifact = goproxy.artifact_for("example.com/m", "v1.0.0", suffix)
        assert artifact.digest == ""
        assert artifact.digest_resolver is None


class TestWhyGoIsNotVerifiedHere:
    """The decision is a measurement, so losing its reason is losing the decision.

    **The consumer already verifies, with a stronger control than a fetch-time
    digest would be.** A Go program carries a committed `go.sum` and the `go`
    command enforces it against every module it downloads -- pinned by the
    program's author rather than asserted by whichever proxy answered. The
    authoritative hash otherwise lives in the Go checksum database, a THIRD
    upstream a sealed deployment cannot reach, which is why the runner sets
    `GOSUMDB=off` (that turns off the database LOOKUP, not the `go.sum` check).

    Without that written down, the only evidence is a 404 against
    `.zip.sha256`, which reads as an unfinished job rather than a closed
    question -- and the next person writes a sumdb client that duplicates the
    consumer's check against an upstream we deliberately stopped reaching.
    """

    def test_the_ecosystem_is_outside_the_required_set(self) -> None:
        """The behavioural half. `_DIGEST_REQUIRED` matches on
        `Artifact.ecosystem`, and `goproxy.ECOSYSTEM` is `"go"` rather than the
        module name -- so this is asserted against the real string the gate sees.
        """
        from terrapod.services.package_cache.substrate import _DIGEST_REQUIRED

        assert goproxy.ECOSYSTEM == "go"
        assert goproxy.ECOSYSTEM not in _DIGEST_REQUIRED

    def test_the_reason_is_recorded_where_the_decision_is_made(self) -> None:
        """A prose gate with exactly one failure mode: somebody deletes the
        explanation while leaving the behaviour.

        It reads `artifact_for`'s own source rather than the module docstring,
        because the comment has to be where the empty digest is written -- that is
        where a reader arrives, and a reason two hundred lines away is a reason
        nobody meets.
        """
        import inspect

        source = inspect.getsource(goproxy.artifact_for)
        for token in ("go.sum", "GOSUMDB"):
            assert token in source, (
                f"{token} is no longer named where the empty digest is written. "
                "That reason is the decision; restore it rather than deleting this."
            )

    def test_the_runner_really_does_turn_the_checksum_database_off(self) -> None:
        """The premise of the reason above, asserted rather than quoted.

        If a future change pointed the runner at a reachable checksum database,
        the argument would still hold on `go.sum` but its second half would be
        stale -- and a stale REASON reads as considered, which is the direction
        that survives review.
        """
        from terrapod.runner.phases.pulumi_deps import go_env

        assert go_env(1234)["GOSUMDB"] == "off"


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

    def test_no_digest_is_carried_but_one_is_resolvable(self) -> None:
        """The flat container publishes none, so the Artifact carries none -- and a
        resolver is attached to fetch it from the registration index on a miss.

        Both halves matter: carrying one would mean an upstream round-trip during
        construction, which happens on every request including a cache hit, and
        attaching none would mean the package is cached unverified.
        """
        artifact = nuget.package_artifact("A", "1.0.0")
        assert artifact.digest == ""
        assert artifact.digest_resolver is not None


class TestNuGetDigestResolution:
    """`resolve_digest`: two hops to the only digest NuGet publishes.

    Shapes taken from a real `api.nuget.org` response rather than documentation:
    the gz-semver2 registration leaf gives `catalogEntry` as a URL, and the digest
    on it is base64 with `packageHashAlgorithm` naming SHA512.
    """

    @staticmethod
    def _client(handler):
        import httpx

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_a_url_catalog_entry_is_followed(self) -> None:
        import httpx

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.path.endswith("/1.0.0.json"):
                return httpx.Response(200, json={"catalogEntry": "https://catalog.test/a.json"})
            return httpx.Response(
                200, json={"packageHash": "YWJjZA==", "packageHashAlgorithm": "SHA512"}
            )

        async with self._client(handler) as client:
            got = await nuget.resolve_digest("A", "1.0.0", client)

        assert got == "sha512-YWJjZA=="
        assert len(seen) == 2, seen

    async def test_an_inline_catalog_entry_needs_no_second_hop(self) -> None:
        """Some registrations inline it. Handled rather than assumed away."""
        import httpx

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "catalogEntry": {
                        "packageHash": "YWJjZA==",
                        "packageHashAlgorithm": "SHA512",
                    }
                },
            )

        async with self._client(handler) as client:
            got = await nuget.resolve_digest("A", "1.0.0", client)

        assert got == "sha512-YWJjZA=="
        assert len(seen) == 1, seen

    async def test_the_published_algorithm_is_used_rather_than_assumed(self) -> None:
        """Guessing SHA-512 would compare it against a SHA-256 computation and
        refuse every package, which is a total outage wearing a verification
        failure's clothes."""
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "catalogEntry": {
                        "packageHash": "YWJjZA==",
                        "packageHashAlgorithm": "SHA256",
                    }
                },
            )

        async with self._client(handler) as client:
            assert await nuget.resolve_digest("A", "1.0.0", client) == "sha256-YWJjZA=="

    @pytest.mark.parametrize(
        "entry",
        [
            {"catalogEntry": {"packageHashAlgorithm": "SHA512"}},
            {"catalogEntry": {"packageHash": "YWJjZA=="}},
            {"catalogEntry": None},
            {},
        ],
    )
    async def test_anything_unreadable_fails_closed(self, entry: dict) -> None:
        """An empty return would read as "nothing published", which for NuGet is
        never true -- so it must raise rather than let the package through."""
        import httpx

        from terrapod.services.package_cache.substrate import UpstreamError

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=entry)

        async with self._client(handler) as client:
            with pytest.raises(UpstreamError):
                await nuget.resolve_digest("A", "1.0.0", client)

    async def test_a_missing_package_is_not_found_rather_than_an_error(self) -> None:
        import httpx

        from terrapod.services.package_cache.substrate import NotFoundUpstream

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        async with self._client(handler) as client:
            with pytest.raises(NotFoundUpstream):
                await nuget.resolve_digest("A", "1.0.0", client)
