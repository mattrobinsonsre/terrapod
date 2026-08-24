"""The PyPI and npm proxy endpoints (#1417)."""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers.package_cache import authenticate_package_request
from terrapod.db.session import get_db
from terrapod.storage import get_storage

BASE = "/api/terrapod/v1/package-cache"
AUTH = {"Authorization": "Bearer test-token"}

PYPI_INDEX = {
    "meta": {"api-version": "1.0"},
    "name": "flask",
    "files": [
        {
            "filename": "flask-3.0.0-py3-none-any.whl",
            "url": "https://files.pythonhosted.org/packages/ab/flask-3.0.0-py3-none-any.whl",
            "hashes": {"sha256": "a" * 64},
        }
    ],
}

NPM_PACKUMENT = {
    "name": "left-pad",
    "versions": {
        "1.3.0": {
            "dist": {
                "tarball": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
                "integrity": "sha512-" + "B" * 86,
            }
        }
    },
}


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        email="a@b.c",
        display_name=None,
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _app(storage=None):
    app = create_app()
    app.dependency_overrides[authenticate_package_request] = lambda: _user()
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_storage] = lambda: storage or AsyncMock()
    return app


async def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestPyPIIndex:
    @patch("terrapod.services.package_cache.pypi.fetch_index")
    async def test_file_urls_are_relative_to_the_index(self, fetch) -> None:
        """The property that means this proxy cannot be misconfigured.

        PEP 691 resolves a relative URL against the index page, so serving files
        from under the index path lets the rewrite be the bare filename — and the
        proxy never has to know, or be told, its own external URL.
        """
        fetch.return_value = PYPI_INDEX

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/pypi/simple/Flask/", headers=AUTH)

        assert r.status_code == 200
        assert r.json()["files"][0]["url"] == "flask-3.0.0-py3-none-any.whl"

    @patch("terrapod.services.package_cache.pypi.fetch_index")
    async def test_hashes_reach_the_client_untouched(self, fetch) -> None:
        fetch.return_value = PYPI_INDEX

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/pypi/simple/flask/", headers=AUTH)

        assert r.json()["files"][0]["hashes"] == {"sha256": "a" * 64}

    @patch("terrapod.services.package_cache.pypi.fetch_index")
    async def test_html_is_served_to_a_client_that_asked_for_it(self, fetch) -> None:
        fetch.return_value = PYPI_INDEX

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/pypi/simple/flask/", headers={**AUTH, "Accept": "text/html"})

        assert r.headers["content-type"].startswith("text/html")
        assert f"#sha256={'a' * 64}" in r.text

    @patch("terrapod.services.package_cache.pypi.fetch_index")
    async def test_json_is_the_default(self, fetch) -> None:
        """pip sends `*/*` on some paths; JSON is the better document to give it."""
        fetch.return_value = PYPI_INDEX

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/pypi/simple/flask/", headers={**AUTH, "Accept": "*/*"})

        assert r.headers["content-type"].startswith("application/vnd.pypi.simple.v1+json")

    @patch("terrapod.services.package_cache.pypi.fetch_index")
    async def test_an_unknown_project_is_404_not_502(self, fetch) -> None:
        from terrapod.services.package_cache.substrate import NotFoundUpstream

        fetch.side_effect = NotFoundUpstream("nope")

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/pypi/simple/nope/", headers=AUTH)

        assert r.status_code == 404

    @patch("terrapod.services.package_cache.pypi.fetch_index")
    async def test_an_unreachable_upstream_is_502(self, fetch) -> None:
        """Distinct from 404: one is worth retrying and the other is not."""
        from terrapod.services.package_cache.substrate import UpstreamError

        fetch.side_effect = UpstreamError("connection refused")

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/pypi/simple/flask/", headers=AUTH)

        assert r.status_code == 502


class TestPyPIFile:
    @patch("terrapod.services.package_cache.substrate.lookup")
    async def test_a_cached_file_redirects_to_storage(self, lookup) -> None:
        record = MagicMock()
        record.storage_key = "cache/packages/pypi/flask/flask-3.0.0.whl"
        lookup.return_value = record
        storage = AsyncMock()
        storage.exists.return_value = True
        storage.presigned_get_url.return_value = MagicMock(url="https://store.test/signed")

        with patch("terrapod.services.package_cache.substrate.touch", new=AsyncMock()):
            async with await _client(_app(storage)) as c:
                r = await c.get(
                    f"{BASE}/pypi/simple/flask/flask-3.0.0.whl",
                    headers=AUTH,
                    follow_redirects=False,
                )

        # Redirected, not proxied: a 500 MB wheel must not pass through the API pod.
        assert r.status_code == 302
        assert r.headers["location"] == "https://store.test/signed"
        # And the object's presence was actually confirmed before promising it.
        storage.exists.assert_awaited_once_with(record.storage_key)

    @patch("terrapod.services.package_cache.pypi.fetch_index")
    @patch("terrapod.services.package_cache.substrate.lookup")
    async def test_a_row_whose_object_vanished_is_not_served(self, lookup, fetch) -> None:
        """The row is not the truth; the store is.

        A pruned bucket or a recreated container leaves rows pointing at objects
        that are gone. Redirecting to one produces a 404 from storage that nobody
        can trace back to a database row — which is exactly what happened during
        this feature's own testing, and read like a client bug.
        """
        record = MagicMock()
        record.storage_key = "cache/packages/pypi/flask/gone.whl"
        lookup.return_value = record
        storage = AsyncMock()
        storage.exists.return_value = False
        # Upstream no longer lists it either, so the re-fetch honestly 404s —
        # what matters is that we did not blindly redirect to the missing object.
        fetch.return_value = {"name": "flask", "files": []}

        async with await _client(_app(storage)) as c:
            r = await c.get(
                f"{BASE}/pypi/simple/flask/gone.whl", headers=AUTH, follow_redirects=False
            )

        assert r.status_code != 302
        assert r.status_code == 404

    @patch("terrapod.services.package_cache.pypi.fetch_index")
    @patch("terrapod.services.package_cache.substrate.lookup")
    async def test_a_file_absent_from_the_index_is_404(self, lookup, fetch) -> None:
        """Never fetch a filename upstream has not published for this project."""
        lookup.return_value = None
        fetch.return_value = PYPI_INDEX

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/pypi/simple/flask/evil-1.0.whl", headers=AUTH)

        assert r.status_code == 404

    @patch("terrapod.api.routers.package_cache.get_or_fetch")
    @patch("terrapod.services.package_cache.pypi.fetch_index")
    @patch("terrapod.services.package_cache.substrate.lookup")
    async def test_sealing_is_503_with_the_setting_named(self, lookup, fetch, fetch_cache) -> None:
        """A 404 would send a developer looking for a package that exists."""
        from terrapod.services.package_cache.substrate import SealedError

        lookup.return_value = None
        fetch.return_value = PYPI_INDEX
        fetch_cache.side_effect = SealedError("not cached and this node is sealed (cache_only)")

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/pypi/simple/flask/flask-3.0.0-py3-none-any.whl", headers=AUTH)

        assert r.status_code == 503
        assert "cache_only" in r.json()["detail"]


class TestNpm:
    @patch("terrapod.services.package_cache.npm.fetch_packument")
    async def test_tarball_urls_are_absolute_and_point_at_us(self, fetch) -> None:
        """npm does not resolve `dist.tarball` relative to the packument."""
        fetch.return_value = NPM_PACKUMENT

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/npm/left-pad", headers=AUTH)

        url = r.json()["versions"]["1.3.0"]["dist"]["tarball"]
        assert url.startswith("http")
        assert url.endswith("/package-cache/npm/left-pad/-/left-pad-1.3.0.tgz")

    @patch("terrapod.services.package_cache.npm.fetch_packument")
    async def test_a_scoped_package_resolves(self, fetch) -> None:
        fetch.return_value = {"name": "@types/node", "versions": {}}

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/npm/@types/node", headers=AUTH)

        assert r.status_code == 200
        assert fetch.call_args.args[0] == "@types/node"

    @patch("terrapod.services.package_cache.substrate.lookup")
    async def test_a_scoped_tarball_route_beats_the_packument_route(self, lookup) -> None:
        """`{package:path}` is greedy and backtracks to the last `/-/`.

        If it did not, `@types/node/-/node-20.1.0.tgz` would be read as a package
        name and every scoped install would fail.
        """
        record = MagicMock()
        record.storage_key = "cache/packages/npm/@types/node/node-20.1.0.tgz"
        lookup.return_value = record
        storage = AsyncMock()
        storage.presigned_get_url.return_value = MagicMock(url="https://store.test/x")

        with patch("terrapod.services.package_cache.substrate.touch", new=AsyncMock()):
            async with await _client(_app(storage)) as c:
                r = await c.get(
                    f"{BASE}/npm/@types/node/-/node-20.1.0.tgz",
                    headers=AUTH,
                    follow_redirects=False,
                )

        assert r.status_code == 302
        assert lookup.call_args.args[2] == "@types/node"
        assert lookup.call_args.args[3] == "node-20.1.0.tgz"

    @patch("terrapod.services.package_cache.npm.fetch_packument")
    async def test_the_abbreviated_accept_is_passed_upstream(self, fetch) -> None:
        """Otherwise every install pulls the full multi-megabyte packument."""
        fetch.return_value = NPM_PACKUMENT

        async with await _client(_app()) as c:
            await c.get(
                f"{BASE}/npm/left-pad",
                headers={**AUTH, "Accept": "application/vnd.npm.install-v1+json"},
            )

        assert fetch.call_args.kwargs["accept"] == "application/vnd.npm.install-v1+json"


class TestDisabled:
    """A switched-off ecosystem is not mounted, so its routes do not exist.

    Asserted by building the application *after* changing the setting, because
    that is when the decision is now made: an ecosystem nobody wants is never
    registered rather than registered and refusing (#1429). Patching the router
    module's `settings` no longer has any effect — the decision moved to
    `engine_gating`, which reads the real settings object.
    """

    @pytest.fixture(autouse=True)
    def _restore(self):
        from terrapod.config import settings

        before = (
            settings.registry.package_cache.enabled,
            settings.registry.package_cache.pypi.enabled,
        )
        yield
        (
            settings.registry.package_cache.enabled,
            settings.registry.package_cache.pypi.enabled,
        ) = before

    async def test_a_disabled_ecosystem_is_404(self) -> None:
        from terrapod.config import settings

        settings.registry.package_cache.enabled = True
        settings.registry.package_cache.pypi.enabled = False

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/pypi/simple/flask/", headers=AUTH)

        assert r.status_code == 404

    async def test_the_whole_cache_can_be_switched_off(self) -> None:
        from terrapod.config import settings

        settings.registry.package_cache.enabled = False

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/npm/left-pad", headers=AUTH)

        assert r.status_code == 404


class TestAuthenticationIsRequired:
    """Every route, walked from the real table rather than a hand-written list.

    An unauthenticated package proxy is an open bandwidth relay for whoever finds
    it, and worse than the container registry's equivalent because pip and npm
    will happily use it without anyone noticing it was meant to be private.
    """

    def _routes(self):
        app = create_app()
        return [route for route in app.routes if "/package-cache/" in getattr(route, "path", "")]

    #: path → (method, url, json body). The method matters: warming is a POST
    #: (#1420), and issuing a GET against it answers 405 without ever consulting
    #: the auth dependency — which would have looked like a passing auth test
    #: while proving nothing about it.
    SAMPLES = {
        "/api/terrapod/v1/package-cache/pypi/simple/{project}/": (
            "GET",
            f"{BASE}/pypi/simple/flask/",
            None,
        ),
        "/api/terrapod/v1/package-cache/pypi/simple/{project}/{filename}": (
            "GET",
            f"{BASE}/pypi/simple/flask/flask-3.0.0.whl",
            None,
        ),
        "/api/terrapod/v1/package-cache/npm/{package:path}": (
            "GET",
            f"{BASE}/npm/left-pad",
            None,
        ),
        "/api/terrapod/v1/package-cache/npm/{package:path}/-/{filename}": (
            "GET",
            f"{BASE}/npm/left-pad/-/left-pad-1.3.0.tgz",
            None,
        ),
        "/api/terrapod/v1/admin/package-cache/warm": (
            "POST",
            "/api/terrapod/v1/admin/package-cache/warm",
            {"packages": [{"ecosystem": "pypi", "name": "requests"}]},
        ),
    }

    def test_every_route_has_a_sample_request(self) -> None:
        """Stops the walk below silently skipping a newly added endpoint."""
        missing = [r.path for r in self._routes() if r.path not in self.SAMPLES]
        assert not missing, f"add a sample request for: {missing}"

    @pytest.mark.parametrize("sample", sorted(SAMPLES.values()))
    async def test_anonymous_is_rejected(self, sample: tuple) -> None:
        method, path, body = sample
        app = create_app()  # no auth override — the real dependency runs
        # The admin route resolves a user before deciding, which needs a session.
        # The proxy routes refuse before touching the database, so this changes
        # nothing for them.
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        async with await _client(app) as c:
            r = await c.request(method, path, json=body)

        if method != "GET":
            # An admin route answers 403 rather than the proxy's Basic challenge:
            # it is not a route pip or npm ever calls, so there is nothing to
            # challenge. What matters is that it refuses.
            assert r.status_code in (401, 403)
            return
        assert r.status_code == 401
        # pip has no bearer option, so the challenge must name Basic or it will
        # not retry with the credentials it was given.
        assert "Basic" in r.headers.get("www-authenticate", "")

    @pytest.mark.parametrize("sample", sorted(SAMPLES.values()))
    async def test_a_bad_credential_is_rejected(self, sample: tuple) -> None:
        method, path, body = sample
        app = create_app()
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        header = base64.b64encode(b"user:not-a-real-token").decode()

        @asynccontextmanager
        async def _session():
            yield AsyncMock()

        with (
            patch("terrapod.db.session.get_db_session", _session),
            patch(
                "terrapod.api.dependencies.validate_api_token",
                new=AsyncMock(return_value=None),
            ),
            patch("terrapod.auth.sessions.get_session", new=AsyncMock(return_value=None)),
        ):
            async with await _client(app) as c:
                r = await c.request(
                    method, path, json=body, headers={"Authorization": f"Basic {header}"}
                )

        assert r.status_code in (401, 403)


class TestSealed:
    """A sealed node serves what it holds and never reaches upstream.

    Verified end to end against a live stack with the upstream pointed at an
    unroutable host — these pin the behaviour so it cannot regress silently.
    """

    @patch("terrapod.api.routers.package_cache.cached_files")
    @patch("terrapod.api.routers.package_cache.sealed")
    @patch("terrapod.services.package_cache.pypi.fetch_index")
    async def test_the_pypi_index_is_built_from_cache_not_upstream(
        self, fetch, is_sealed, cached
    ) -> None:
        is_sealed.return_value = True
        row = MagicMock()
        row.filename = "flask-3.0.0-py3-none-any.whl"
        row.digest = f"sha256:{'a' * 64}"
        cached.return_value = [row]

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/pypi/simple/flask/", headers=AUTH)

        assert r.status_code == 200
        assert [f["filename"] for f in r.json()["files"]] == [row.filename]
        # The whole point: upstream was never asked.
        fetch.assert_not_called()

    @patch("terrapod.api.routers.package_cache.cached_files")
    @patch("terrapod.api.routers.package_cache.sealed")
    async def test_an_uncached_project_names_the_setting(self, is_sealed, cached) -> None:
        """Not a bare 404: the operator must be able to tell this from a typo."""
        is_sealed.return_value = True
        cached.return_value = []

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/pypi/simple/requests/", headers=AUTH)

        assert r.status_code == 404
        assert "cache_only" in r.json()["detail"]

    @patch("terrapod.api.routers.package_cache.cached_filenames")
    @patch("terrapod.api.routers.package_cache.load_document")
    @patch("terrapod.api.routers.package_cache.sealed")
    @patch("terrapod.services.package_cache.npm.fetch_packument")
    async def test_the_packument_comes_from_cache_and_is_restricted(
        self, fetch, is_sealed, load, held
    ) -> None:
        import json as _json

        is_sealed.return_value = True
        load.return_value = _json.dumps(
            {
                "name": "left-pad",
                "dist-tags": {"latest": "1.3.0"},
                "versions": {
                    "1.2.0": {"dist": {"tarball": "https://up.test/a", "integrity": "sha512-x"}},
                    "1.3.0": {"dist": {"tarball": "https://up.test/b", "integrity": "sha512-y"}},
                },
            }
        ).encode()
        held.return_value = ["left-pad-1.2.0.tgz"]

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/npm/left-pad", headers=AUTH)

        assert r.status_code == 200
        body = r.json()
        # Only the cached version survives, and `latest` follows it rather than
        # pointing at a version npm would then fail to fetch.
        assert list(body["versions"]) == ["1.2.0"]
        assert body["dist-tags"]["latest"] == "1.2.0"
        fetch.assert_not_called()

    @patch("terrapod.api.routers.package_cache.load_document")
    @patch("terrapod.api.routers.package_cache.sealed")
    async def test_an_uncached_packument_names_the_setting(self, is_sealed, load) -> None:
        is_sealed.return_value = True
        load.return_value = None

        async with await _client(_app()) as c:
            r = await c.get(f"{BASE}/npm/lodash", headers=AUTH)

        assert r.status_code == 404
        assert "cache_only" in r.json()["detail"]
