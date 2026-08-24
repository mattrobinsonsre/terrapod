"""What warming actually caches (#1420).

Two properties decide whether a warmed cache can serve a sealed install, and
both are easy to get subtly wrong in a way no status field would reveal — the
job would report success and the install would fail after the network was cut.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from terrapod.services.warm_ahead import WarmItem, _warm_npm, _warm_pypi


class TestNpm:
    """The packument is not an optimisation.

    A sealed node cannot serve `npm install` without it — the dependency ranges
    live there and nowhere else — so warming a tarball alone produces a cache
    that looks warm and cannot answer.
    """

    @patch("terrapod.services.package_cache.substrate.get_or_fetch", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.substrate.store_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.npm.fetch_packument", new_callable=AsyncMock)
    async def test_the_packument_is_cached_too(self, packument, store, fetch) -> None:
        packument.return_value = {
            "dist-tags": {"latest": "1.3.0"},
            "versions": {"1.3.0": {"dist": {"tarball": "https://u/left-pad-1.3.0.tgz"}}},
        }

        outcome = await _warm_npm(
            AsyncMock(), AsyncMock(), WarmItem(ecosystem="npm", name="left-pad")
        )

        assert outcome.ok
        store.assert_awaited_once()
        fetch.assert_awaited_once()

    @patch("terrapod.services.package_cache.substrate.get_or_fetch", new_callable=AsyncMock)
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

    @patch("terrapod.services.package_cache.substrate.get_or_fetch", new_callable=AsyncMock)
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

        outcome = await _warm_pypi(
            AsyncMock(), AsyncMock(), WarmItem(ecosystem="pypi", name="pkg", version="1.0")
        )

        assert outcome.ok
        # Three files for 1.0, and the 0.9 sdist left alone.
        assert fetch.await_count == 3

    @patch("terrapod.services.package_cache.substrate.get_or_fetch", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.pypi.fetch_index", new_callable=AsyncMock)
    async def test_a_version_upstream_does_not_have_is_reported(self, index, fetch) -> None:
        index.return_value = {"files": [{"filename": "pkg-0.9.tar.gz", "url": "https://u/x"}]}

        outcome = await _warm_pypi(
            AsyncMock(), AsyncMock(), WarmItem(ecosystem="pypi", name="pkg", version="2.0")
        )

        assert not outcome.ok
        assert "2.0" in outcome.detail
        fetch.assert_not_awaited()
