"""What warming actually caches (#1420, every ecosystem since #2486).

Two properties decide whether a warmed cache can serve a sealed install, and
both are easy to get subtly wrong in a way no status field would reveal — the
job would report success and the install would fail after the network was cut.

The second of them is the subject of most of this file. **An ecosystem's
artifact is not sufficient on its own**: four of the six serve an index document
a sealed node cannot obtain any other way, and a warm that cached only the
tarball produces a cache that looks full and cannot answer. So each adapter's
test asserts the DOCUMENTS as well as the bytes, because the bytes are the half
that is obvious.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from terrapod.services.package_cache.substrate import NotFoundUpstream
from terrapod.services.warm_ahead import (
    _HANDLERS,
    ECOSYSTEMS,
    PACKAGE_ECOSYSTEMS,
    RESOLVES_LATEST,
    WarmItem,
    _warm_galaxy,
    _warm_go,
    _warm_npm,
    _warm_nuget,
    _warm_one,
    _warm_pulumi,
    _warm_pypi,
    effective_sources,
    resolve_source,
)


def _miss():
    """`lookup_present` saying the artifact is not held — so `_ensure` fetches."""
    return patch(
        "terrapod.services.package_cache.substrate.lookup_present",
        new_callable=AsyncMock,
        return_value=None,
    )


def _hit():
    """`lookup_present` saying it IS held — so `_ensure` reports no fetch."""
    return patch(
        "terrapod.services.package_cache.substrate.lookup_present",
        new_callable=AsyncMock,
        return_value=object(),
    )


class TestTheSetOfEcosystems:
    """The three places that enumerate ecosystems must agree.

    They are deliberately separate — a cached artifact's ecosystem, a gated
    capability and a warmable thing are three different questions — which is
    exactly why nothing stops them drifting apart. That drift is what #2486
    was: the column documented six, the capabilities table carried six, and the
    warmer dispatched on two of them plus `oci`.
    """

    def test_every_package_ecosystem_has_a_handler(self) -> None:
        missing = [e for e in PACKAGE_ECOSYSTEMS if e not in _HANDLERS]
        assert not missing, f"no warm adapter for {missing}"

    def test_every_handler_is_an_ecosystem(self) -> None:
        assert set(_HANDLERS) == set(ECOSYSTEMS)

    def test_it_matches_the_capabilities_table(self) -> None:
        from terrapod.services.capabilities import CAPABILITIES

        assert set(ECOSYSTEMS) == set(CAPABILITIES)

    def test_it_matches_the_column_the_cache_is_keyed_by(self) -> None:
        # Read from the SOURCE, because the list lives in a `#:` comment above
        # the column rather than in a SQLAlchemy `comment=` -- so
        # `CachedPackageFile.ecosystem.comment` is empty and asserting on it
        # would pass against nothing.
        import pathlib

        from terrapod.db import models

        source = pathlib.Path(models.__file__).read_text()
        start = source.index("class CachedPackageFile(")
        block = source[start : source.index("\n    name: Mapped[str]", start)]
        for ecosystem in PACKAGE_ECOSYSTEMS:
            assert f'"{ecosystem}"' in block, f"{ecosystem} is not in the column's own comment"

    def test_only_pulumi_cannot_resolve_a_latest(self) -> None:
        # Not a free choice: the Pulumi download protocol publishes no index at
        # all, so a bare name has nothing to resolve against. Every other
        # ecosystem has a document that names a newest.
        assert set(ECOSYSTEMS) - RESOLVES_LATEST == {"pulumi"}


class TestNpm:
    """The packument is not an optimisation.

    A sealed node cannot serve `npm install` without it — the dependency ranges
    live there and nowhere else — so warming a tarball alone produces a cache
    that looks warm and cannot answer.
    """

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.substrate.store_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.npm.fetch_packument", new_callable=AsyncMock)
    async def test_the_packument_is_cached_too(self, packument, store, fetch) -> None:
        packument.return_value = {
            "dist-tags": {"latest": "1.3.0"},
            "versions": {"1.3.0": {"dist": {"tarball": "https://u/left-pad-1.3.0.tgz"}}},
        }

        with _miss():
            outcome = await _warm_npm(
                AsyncMock(), AsyncMock(), WarmItem(ecosystem="npm", name="left-pad")
            )

        assert outcome.ok
        store.assert_awaited_once()
        fetch.assert_awaited_once()

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.substrate.store_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.npm.fetch_packument", new_callable=AsyncMock)
    async def test_an_absent_version_is_reported_not_raised(self, packument, store, f) -> None:
        packument.return_value = {"dist-tags": {"latest": "1.3.0"}, "versions": {}}

        outcome = await _warm_npm(
            AsyncMock(), AsyncMock(), WarmItem(ecosystem="npm", name="left-pad", version="9.9.9")
        )

        assert not outcome.ok
        assert "9.9.9" in outcome.detail


class TestPypi:
    """Every file for a version, not the one wheel we happen to expect.

    Which wheel pip selects depends on the interpreter and platform doing the
    installing. Guessing is exactly the discover-after-sealing this exists to
    prevent.
    """

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.pypi.fetch_index", new_callable=AsyncMock)
    async def test_all_wheels_for_the_version_are_cached(self, index, fetch) -> None:
        index.return_value = {
            "files": [
                {"filename": "pkg-1.0-cp311-cp311-manylinux_x86_64.whl", "url": "https://u/a"},
                {"filename": "pkg-1.0-cp311-cp311-macosx_arm64.whl", "url": "https://u/b"},
                {"filename": "pkg-1.0.tar.gz", "url": "https://u/c"},
                {"filename": "pkg-0.9.tar.gz", "url": "https://u/old"},
            ]
        }

        with _miss():
            outcome = await _warm_pypi(
                AsyncMock(), AsyncMock(), WarmItem(ecosystem="pypi", name="pkg", version="1.0")
            )

        assert outcome.ok
        # Three files for 1.0, and the 0.9 sdist left alone.
        assert fetch.await_count == 3

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.pypi.fetch_index", new_callable=AsyncMock)
    async def test_a_version_upstream_does_not_have_is_reported(self, index, fetch) -> None:
        index.return_value = {"files": [{"filename": "pkg-0.9.tar.gz", "url": "https://u/x"}]}

        outcome = await _warm_pypi(
            AsyncMock(), AsyncMock(), WarmItem(ecosystem="pypi", name="pkg", version="2.0")
        )

        assert not outcome.ok
        assert "2.0" in outcome.detail
        fetch.assert_not_awaited()


class TestAlreadyCachedIsItsOwnAnswer:
    """ "Fetched nothing" and "fetched everything" must not read alike (#2486).

    Warming is idempotent and resumable by design, so re-running a submission
    is the normal way to close the last gaps. Without this distinction the
    second run reports exactly what the first did, and an operator cannot see
    that it converged — which is the whole question they are asking before they
    seal the node.
    """

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.pypi.fetch_index", new_callable=AsyncMock)
    async def test_a_fully_cached_item_reports_files_without_fetches(self, index, fetch) -> None:
        index.return_value = {"files": [{"filename": "pkg-1.0.tar.gz", "url": "https://u/c"}]}

        with _hit():
            outcome = await _warm_pypi(
                AsyncMock(), AsyncMock(), WarmItem(ecosystem="pypi", name="pkg", version="1.0")
            )

        assert outcome.ok
        assert outcome.files == 1
        assert outcome.fetched == 0
        fetch.assert_not_awaited()

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.pypi.fetch_index", new_callable=AsyncMock)
    async def test_a_cold_item_reports_the_same_count_as_fetches(self, index, fetch) -> None:
        index.return_value = {"files": [{"filename": "pkg-1.0.tar.gz", "url": "https://u/c"}]}

        with _miss():
            outcome = await _warm_pypi(
                AsyncMock(), AsyncMock(), WarmItem(ecosystem="pypi", name="pkg", version="1.0")
            )

        assert outcome.files == 1
        assert outcome.fetched == 1


class TestGalaxy:
    """A collection install resolves before it downloads.

    `ansible-galaxy` asks what versions exist, then for one version's detail,
    and follows the `download_url` in it. A sealed node holding only the tarball
    answers the first two with nothing, so the install fails before it reaches
    the bytes that were warmed.
    """

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.substrate.store_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_version", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_versions", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_collection", new_callable=AsyncMock)
    async def test_the_three_index_documents_are_cached_too(
        self, collection, versions, version, store, fetch
    ) -> None:
        collection.return_value = {"highest_version": {"version": "9.2.0"}}
        versions.return_value = {"data": [{"version": "9.2.0"}]}
        version.return_value = {
            "download_url": "https://u/community-general-9.2.0.tar.gz",
            "artifact": {"sha256": "abc"},
        }

        with _miss():
            outcome = await _warm_galaxy(
                AsyncMock(),
                AsyncMock(),
                WarmItem(ecosystem="galaxy", name="community.general", version="9.2.0"),
            )

        assert outcome.ok, outcome.detail
        # collection.json, versions.json and version-9.2.0.json.
        assert store.await_count == 3
        stored = {call.args[2].filename for call in store.await_args_list}
        assert stored == {"collection.json", "versions.json", "version-9.2.0.json"}
        fetch.assert_awaited_once()
        assert outcome.files == 4

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.substrate.store_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_version", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_versions", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_collection", new_callable=AsyncMock)
    async def test_no_version_takes_the_highest_upstream_names(
        self, collection, versions, version, store, fetch
    ) -> None:
        collection.return_value = {"highest_version": {"version": "10.1.0"}}
        versions.return_value = {"data": []}
        version.return_value = {"download_url": "https://u/x.tar.gz", "artifact": {"sha256": "a"}}

        with _miss():
            outcome = await _warm_galaxy(
                AsyncMock(), AsyncMock(), WarmItem(ecosystem="galaxy", name="community.general")
            )

        assert outcome.ok
        assert outcome.ref == "community.general@10.1.0"
        assert version.await_args.args[2] == "10.1.0"

    async def test_a_name_without_a_namespace_is_refused_before_any_fetch(self) -> None:
        outcome = await _warm_galaxy(
            AsyncMock(), AsyncMock(), WarmItem(ecosystem="galaxy", name="general")
        )

        assert not outcome.ok
        assert "namespace.name" in outcome.detail

    @patch("terrapod.services.package_cache.substrate.store_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_versions", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_collection", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_version", new_callable=AsyncMock)
    async def test_an_absent_version_is_reported_not_raised(
        self, version, collection, versions, store
    ) -> None:
        collection.return_value = {"highest_version": {"version": "9.2.0"}}
        versions.return_value = {"data": []}
        version.side_effect = NotFoundUpstream("x")

        outcome = await _warm_galaxy(
            AsyncMock(),
            AsyncMock(),
            WarmItem(ecosystem="galaxy", name="community.general", version="99.0.0"),
        )

        assert not outcome.ok
        assert "99.0.0" in outcome.detail


class TestGo:
    """`.mod` is read long before any source is downloaded.

    The toolchain resolves the dependency graph from `.mod` files, so a cache
    holding only `.zip` cannot start a sealed `go mod download` at all.
    """

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.substrate.store_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.goproxy.fetch_document", new_callable=AsyncMock)
    async def test_all_three_suffixes_are_cached(self, document, store, fetch) -> None:
        document.return_value = b"v1.2.3\n"

        with _miss():
            outcome = await _warm_go(
                AsyncMock(),
                AsyncMock(),
                WarmItem(ecosystem="go", name="example.com/a/b", version="v1.2.3"),
            )

        assert outcome.ok, outcome.detail
        suffixes = {call.args[2].filename for call in fetch.await_args_list}
        assert suffixes == {"v1.2.3.info", "v1.2.3.mod", "v1.2.3.zip"}

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.substrate.store_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.goproxy.fetch_document", new_callable=AsyncMock)
    async def test_no_version_resolves_through_at_latest(self, document, store, fetch) -> None:
        document.side_effect = [b"v1.0.0\nv1.2.3\n", json.dumps({"Version": "v1.2.3"}).encode()]

        with _miss():
            outcome = await _warm_go(
                AsyncMock(), AsyncMock(), WarmItem(ecosystem="go", name="example.com/a/b")
            )

        assert outcome.ok, outcome.detail
        assert outcome.ref == "example.com/a/b@v1.2.3"

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.substrate.store_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.goproxy.fetch_document", new_callable=AsyncMock)
    async def test_a_module_with_no_list_still_warms_its_version(
        self, document, store, fetch
    ) -> None:
        # Normal rather than exceptional: the toolchain probes parent prefixes,
        # and a module may publish no list. The version files are what the
        # install needs, so a missing list must not fail the item.
        document.side_effect = NotFoundUpstream("example.com/a/b")

        with _miss():
            outcome = await _warm_go(
                AsyncMock(),
                AsyncMock(),
                WarmItem(ecosystem="go", name="example.com/a/b", version="v1.2.3"),
            )

        assert outcome.ok, outcome.detail
        assert fetch.await_count == 3

    async def test_a_version_without_the_leading_v_is_refused(self) -> None:
        outcome = await _warm_go(
            AsyncMock(),
            AsyncMock(),
            WarmItem(ecosystem="go", name="example.com/a/b", version="1.2.3"),
        )

        assert not outcome.ok
        assert "leading v" in outcome.detail


class TestNuget:
    """`restore` resolves against the versions document before it downloads."""

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.substrate.store_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.nuget.fetch_versions", new_callable=AsyncMock)
    async def test_the_versions_document_is_cached_too(self, versions, store, fetch) -> None:
        versions.return_value = json.dumps({"versions": ["12.0.0", "13.0.3"]}).encode()

        with _miss():
            outcome = await _warm_nuget(
                AsyncMock(),
                AsyncMock(),
                WarmItem(ecosystem="nuget", name="Newtonsoft.Json", version="13.0.3"),
            )

        assert outcome.ok, outcome.detail
        store.assert_awaited_once()
        fetch.assert_awaited_once()

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.substrate.store_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.nuget.fetch_versions", new_callable=AsyncMock)
    async def test_no_version_takes_upstreams_last_entry(self, versions, store, fetch) -> None:
        # Ascending, and taken rather than sorted: deciding pre-release ordering
        # ourselves would disagree with the registry that published the list.
        versions.return_value = json.dumps({"versions": ["12.0.0", "13.0.3"]}).encode()

        with _miss():
            outcome = await _warm_nuget(
                AsyncMock(), AsyncMock(), WarmItem(ecosystem="nuget", name="Newtonsoft.Json")
            )

        assert outcome.ok
        # Lowercased, which is how the protocol addresses a package — so one
        # cache entry rather than two copies of identical bytes.
        assert outcome.ref == "newtonsoft.json@13.0.3"


class TestPulumi:
    """A plugin is per-platform, and has no index at all."""

    async def test_a_bare_name_is_refused_with_the_reason(self) -> None:
        outcome = await _warm_pulumi(
            AsyncMock(), AsyncMock(), WarmItem(ecosystem="pulumi", name="aws")
        )

        assert not outcome.ok
        assert "no index" in outcome.detail

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    async def test_both_runner_platforms_are_warmed_by_default(self, fetch) -> None:
        with _miss():
            outcome = await _warm_pulumi(
                AsyncMock(),
                AsyncMock(),
                WarmItem(ecosystem="pulumi", name="aws", version="6.0.0"),
            )

        assert outcome.ok, outcome.detail
        names = {call.args[2].filename for call in fetch.await_args_list}
        assert names == {
            "pulumi-resource-aws-v6.0.0-linux-amd64.tar.gz",
            "pulumi-resource-aws-v6.0.0-linux-arm64.tar.gz",
        }

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    async def test_a_kind_prefix_is_honoured(self, fetch) -> None:
        with _miss():
            outcome = await _warm_pulumi(
                AsyncMock(),
                AsyncMock(),
                WarmItem(
                    ecosystem="pulumi",
                    name="language:python",
                    version="3.100.0",
                    platforms=["linux/amd64"],
                ),
            )

        assert outcome.ok, outcome.detail
        assert fetch.await_args.args[2].filename == (
            "pulumi-language-python-v3.100.0-linux-amd64.tar.gz"
        )

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    async def test_a_partial_success_names_the_platform_that_failed(self, fetch) -> None:
        # Reporting this as a plain success is the half-warmed cache again, one
        # architecture at a time.
        with _miss():
            outcome = await _warm_pulumi(
                AsyncMock(),
                AsyncMock(),
                WarmItem(
                    ecosystem="pulumi",
                    name="aws",
                    version="6.0.0",
                    platforms=["linux/amd64", "nonsense"],
                ),
            )

        assert outcome.ok
        assert outcome.files == 1
        assert "nonsense" in outcome.detail


class TestSourceResolution:
    """A warm may be sourced from somewhere other than the serving upstream."""

    def test_no_source_is_the_configured_upstream(self) -> None:
        from terrapod.config import settings

        assert resolve_source("pypi", "") == settings.registry.package_cache.pypi.upstream

    def test_a_declared_label_resolves_to_its_url(self) -> None:
        from terrapod.config import settings

        settings.registry.package_cache.pypi.mirrors["hub"] = "https://hub.example/pypi"
        try:
            assert resolve_source("pypi", "hub") == "https://hub.example/pypi"
        finally:
            settings.registry.package_cache.pypi.mirrors.pop("hub")

    def test_an_undeclared_label_raises_rather_than_falling_back(self) -> None:
        # Falling back would fill the cache from the wrong place and report
        # success, which on a node about to be sealed is the expensive wrong.
        with pytest.raises(ValueError, match="no source named 'hub'"):
            resolve_source("pypi", "hub")

    def test_oci_takes_no_label_because_it_already_chooses_per_reference(self) -> None:
        with pytest.raises(ValueError, match="registry.oci.upstreams"):
            resolve_source("oci", "anything")

    async def test_an_undeclared_label_fails_the_item_without_fetching(self) -> None:
        outcome = await _warm_one(
            AsyncMock(),
            AsyncMock(),
            WarmItem(ecosystem="pypi", name="pkg", version="1.0", source="nope"),
        )

        assert not outcome.ok
        assert "no source named 'nope'" in outcome.detail

    async def test_an_unknown_ecosystem_names_the_ones_that_exist(self) -> None:
        outcome = await _warm_one(
            AsyncMock(), AsyncMock(), WarmItem(ecosystem="cargo", name="serde")
        )

        assert not outcome.ok
        assert "galaxy" in outcome.detail and "nuget" in outcome.detail

    @patch("terrapod.services.package_cache.substrate.fetch_and_cache", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.pypi.fetch_index", new_callable=AsyncMock)
    async def test_the_chosen_base_reaches_the_adapter(self, index, fetch) -> None:
        # The index is what carries the artifact URLs for pypi, npm and galaxy,
        # so fetching it from the mirror is what points the artifact fetch there
        # — nothing is rewritten afterwards.
        from terrapod.config import settings

        index.return_value = {"files": [{"filename": "pkg-1.0.tar.gz", "url": "https://m/c"}]}
        settings.registry.package_cache.pypi.mirrors["hub"] = "https://hub.example/pypi"
        try:
            with _miss():
                outcome = await _warm_one(
                    AsyncMock(),
                    AsyncMock(),
                    WarmItem(ecosystem="pypi", name="pkg", version="1.0", source="hub"),
                )
        finally:
            settings.registry.package_cache.pypi.mirrors.pop("hub")

        assert outcome.ok, outcome.detail
        assert index.await_args.kwargs["base"] == "https://hub.example/pypi"
        assert outcome.source == "https://hub.example/pypi"

    def test_effective_sources_covers_every_warmable_thing(self) -> None:
        sources = effective_sources()

        assert set(sources) == set(ECOSYSTEMS)
        assert sources["pypi"]["upstream"]
        # `oci` has no single upstream to show: the reference's own first path
        # component names the host.
        assert sources["oci"]["upstream"] == ""
        assert sources["pulumi"]["resolves_latest"] is False
