"""npm packument handling (#1417).

`dist.integrity` is what makes this proxy safe rather than merely convenient: npm
checks the tarball it receives against upstream's sha512 SRI, so a substituted
artifact fails in the client. These tests exist mostly to make sure a future
change to the rewrite cannot quietly remove that check.
"""

from __future__ import annotations

from terrapod.services.package_cache import npm

PACKUMENT = {
    "name": "left-pad",
    "dist-tags": {"latest": "1.3.0"},
    "versions": {
        "1.2.0": {
            "name": "left-pad",
            "version": "1.2.0",
            "dependencies": {"ms": "^2.0.0"},
            "dist": {
                "tarball": "https://registry.npmjs.org/left-pad/-/left-pad-1.2.0.tgz",
                "integrity": "sha512-" + "A" * 86,
                "shasum": "c" * 40,
            },
        },
        "1.3.0": {
            "name": "left-pad",
            "version": "1.3.0",
            "dist": {
                "tarball": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
                "integrity": "sha512-" + "B" * 86,
            },
        },
    },
}

SCOPED = {
    "name": "@types/node",
    "versions": {
        "20.1.0": {
            "dist": {
                "tarball": "https://registry.npmjs.org/@types/node/-/node-20.1.0.tgz",
                "integrity": "sha512-" + "C" * 86,
            }
        }
    },
}


class TestTarballNames:
    def test_scoped_packages_drop_the_scope_like_npm_does(self) -> None:
        # npm serves @types/node's tarball as node-20.1.0.tgz. Matching the
        # convention keeps our URLs indistinguishable in shape from a registry's.
        assert npm.tarball_filename("@types/node", "20.1.0") == "node-20.1.0.tgz"

    def test_unscoped_packages_keep_their_name(self) -> None:
        assert npm.tarball_filename("left-pad", "1.3.0") == "left-pad-1.3.0.tgz"


class TestRewrite:
    def test_every_tarball_points_at_us(self) -> None:
        rewritten = npm.rewrite(PACKUMENT, "https://tp.test/npm")
        for version in ("1.2.0", "1.3.0"):
            url = rewritten["versions"][version]["dist"]["tarball"]
            assert url == f"https://tp.test/npm/left-pad/-/left-pad-{version}.tgz"

    def test_integrity_is_never_touched(self) -> None:
        """The client's only defence against a substituted tarball."""
        rewritten = npm.rewrite(PACKUMENT, "https://tp.test/npm")
        assert rewritten["versions"]["1.2.0"]["dist"]["integrity"] == "sha512-" + "A" * 86
        assert rewritten["versions"]["1.2.0"]["dist"]["shasum"] == "c" * 40

    def test_dependency_ranges_and_dist_tags_survive(self) -> None:
        # Rewriting a range would change what gets installed, which is the one
        # thing a cache must never do.
        rewritten = npm.rewrite(PACKUMENT, "https://tp.test/npm")
        assert rewritten["versions"]["1.2.0"]["dependencies"] == {"ms": "^2.0.0"}
        assert rewritten["dist-tags"] == {"latest": "1.3.0"}

    def test_the_original_is_not_mutated(self) -> None:
        """The upstream document may be cached or reused; rewriting copies."""
        npm.rewrite(PACKUMENT, "https://tp.test/npm")
        assert PACKUMENT["versions"]["1.3.0"]["dist"]["tarball"].startswith(
            "https://registry.npmjs.org/"
        )

    def test_a_scoped_package_keeps_its_scope_in_the_url(self) -> None:
        rewritten = npm.rewrite(SCOPED, "https://tp.test/npm")
        assert (
            rewritten["versions"]["20.1.0"]["dist"]["tarball"]
            == "https://tp.test/npm/@types/node/-/node-20.1.0.tgz"
        )

    def test_a_packument_without_versions_is_returned_unchanged(self) -> None:
        """A 404-shaped or error document must not raise on the way through."""
        assert npm.rewrite({"error": "Not found"}, "https://tp.test/npm") == {"error": "Not found"}


class TestVersionLookup:
    def test_finds_a_version_by_tarball_filename(self) -> None:
        found = npm.find_version(PACKUMENT, "left-pad-1.2.0.tgz")
        assert found is not None and found[0] == "1.2.0"

    def test_finds_a_scoped_version(self) -> None:
        found = npm.find_version(SCOPED, "node-20.1.0.tgz")
        assert found is not None and found[0] == "20.1.0"

    def test_returns_none_for_an_unknown_tarball(self) -> None:
        assert npm.find_version(PACKUMENT, "left-pad-9.9.9.tgz") is None

    def test_artifact_uses_upstream_tarball_and_integrity(self) -> None:
        artifact = npm.artifact_for("left-pad", "1.2.0", PACKUMENT["versions"]["1.2.0"])
        assert artifact.upstream_url.startswith("https://registry.npmjs.org/")
        assert artifact.digest == "sha512-" + "A" * 86
        assert artifact.filename == "left-pad-1.2.0.tgz"

    def test_a_pre_integrity_package_falls_back_to_shasum(self) -> None:
        """npm published sha1 shasums long before SRI; those packages still install."""
        entry = {"dist": {"tarball": "https://e.test/x.tgz", "shasum": "d" * 40}}
        assert npm.artifact_for("x", "1.0.0", entry).digest == f"sha1:{'d' * 40}"


class TestSealedPackument:
    """What a sealed node serves: the cached packument, minus what it cannot serve.

    Leaving an uncached version listed lets npm resolve to it and then fail
    fetching the tarball — which npm reports as a *network* error, sending an
    operator to look at their firewall rather than at what they forgot to warm.
    """

    def test_uncached_versions_are_removed(self) -> None:
        restricted = npm.restrict_to_cached(PACKUMENT, {"left-pad-1.2.0.tgz"})
        assert list(restricted["versions"]) == ["1.2.0"]

    def test_dist_tags_never_point_at_a_removed_version(self) -> None:
        # `latest` was 1.3.0, which is not cached; pointing at it would make npm
        # ask for something the document no longer describes.
        restricted = npm.restrict_to_cached(PACKUMENT, {"left-pad-1.2.0.tgz"})
        assert restricted["dist-tags"]["latest"] == "1.2.0"

    def test_a_surviving_latest_is_left_alone(self) -> None:
        restricted = npm.restrict_to_cached(PACKUMENT, {"left-pad-1.2.0.tgz", "left-pad-1.3.0.tgz"})
        assert restricted["dist-tags"]["latest"] == "1.3.0"

    def test_integrity_survives_the_restriction(self) -> None:
        restricted = npm.restrict_to_cached(PACKUMENT, {"left-pad-1.2.0.tgz"})
        assert restricted["versions"]["1.2.0"]["dist"]["integrity"] == "sha512-" + "A" * 86

    def test_nothing_cached_yields_an_empty_but_valid_document(self) -> None:
        """npm should say "no matching version", not fail parsing."""
        restricted = npm.restrict_to_cached(PACKUMENT, set())
        assert restricted["versions"] == {}
        assert restricted["dist-tags"] == {}

    def test_a_scoped_package_matches_on_its_tarball_name(self) -> None:
        restricted = npm.restrict_to_cached(SCOPED, {"node-20.1.0.tgz"})
        assert list(restricted["versions"]) == ["20.1.0"]
