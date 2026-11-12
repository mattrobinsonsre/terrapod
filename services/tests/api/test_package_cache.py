"""The PyPI and npm proxy endpoints (#1417)."""

from __future__ import annotations

import base64
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.prefixes import canonical_path
from terrapod.api.routers.package_cache import authenticate_package_request
from terrapod.db.session import get_db
from terrapod.services.package_cache import galaxy as galaxy_service
from terrapod.storage import get_storage

BASE = "/api/v1/package-cache"
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
    registered rather than registered and refusing. Patching the router module's
    `settings` no longer has any effect — the decision lives in
    `services/capabilities.py`, which reads the real settings object.
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
        # `package-cache`, not `/package-cache/`: the trailing slash excluded any
        # route whose path ENDS at the cache -- which is `GET /admin/package-cache`,
        # the listing, i.e. the first route an operator reaches and the one most
        # worth knowing refuses an anonymous caller. It was outside this gate
        # entirely until #2474 widened the match.
        return [route for route in app.routes if "package-cache" in getattr(route, "path", "")]

    #: path → (method, url, json body). The method matters: warming is a POST
    #: (#1420), and issuing a GET against it answers 405 without ever consulting
    #: the auth dependency — which would have looked like a passing auth test
    #: while proving nothing about it.
    SAMPLES = {
        "/api/v1/package-cache/pypi/simple/{project}/": (
            "GET",
            f"{BASE}/pypi/simple/flask/",
            None,
        ),
        "/api/v1/package-cache/pypi/simple/{project}/{filename}": (
            "GET",
            f"{BASE}/pypi/simple/flask/flask-3.0.0.whl",
            None,
        ),
        "/api/v1/package-cache/npm/{package:path}": (
            "GET",
            f"{BASE}/npm/left-pad",
            None,
        ),
        "/api/v1/package-cache/npm/{package:path}/-/{filename}": (
            "GET",
            f"{BASE}/npm/left-pad/-/left-pad-1.3.0.tgz",
            None,
        ),
        "/api/v1/package-cache/galaxy/": (
            "GET",
            f"{BASE}/galaxy/",
            None,
        ),
        "/api/v1/package-cache/galaxy/v3/collections/{namespace}/{name}/": (
            "GET",
            f"{BASE}/galaxy/v3/collections/community/general/",
            None,
        ),
        "/api/v1/package-cache/galaxy/v3/collections/{namespace}/{name}/versions/": (
            "GET",
            f"{BASE}/galaxy/v3/collections/community/general/versions/",
            None,
        ),
        "/api/v1/package-cache/galaxy/v3/collections/{namespace}/{name}/versions/{version}/": (
            "GET",
            f"{BASE}/galaxy/v3/collections/community/general/versions/9.0.0/",
            None,
        ),
        "/api/v1/package-cache/galaxy/v3/collections/{namespace}/{name}"
        "/versions/{version}/download/{filename}": (
            "GET",
            f"{BASE}/galaxy/v3/collections/community/general/versions/9.0.0/download/"
            "community-general-9.0.0.tar.gz",
            None,
        ),
        "/api/v1/package-cache/galaxy/v3/artifacts/collections/": (
            "POST",
            f"{BASE}/galaxy/v3/artifacts/collections/",
            None,
        ),
        "/api/v1/package-cache/galaxy/v3/imports/collections/{import_id}/": (
            "GET",
            f"{BASE}/galaxy/v3/imports/collections/01a06dfc-9451-798c-bed0-348d71a4ac26/",
            None,
        ),
        "/api/v1/package-cache/galaxy/v3/collections/{namespace}/{name}"
        "/versions/{version}/signature": (
            "PUT",
            f"{BASE}/galaxy/v3/collections/acme/widgets/versions/1.0.0/signature",
            None,
        ),
        "/api/v1/package-cache/pulumi/{filename}": (
            "GET",
            f"{BASE}/pulumi/pulumi-resource-random-v4.16.3-linux-amd64.tar.gz",
            None,
        ),
        "/api/v1/package-cache/go/{module:path}/@v/list": (
            "GET",
            f"{BASE}/go/example.com/m/@v/list",
            None,
        ),
        "/api/v1/package-cache/go/{module:path}/@latest": (
            "GET",
            f"{BASE}/go/example.com/m/@latest",
            None,
        ),
        "/api/v1/package-cache/go/{module:path}/@v/{file}": (
            "GET",
            f"{BASE}/go/example.com/m/@v/v1.0.0.info",
            None,
        ),
        "/api/v1/package-cache/nuget/index.json": (
            "GET",
            f"{BASE}/nuget/index.json",
            None,
        ),
        "/api/v1/package-cache/nuget/flat/{package_id}/index.json": (
            "GET",
            f"{BASE}/nuget/flat/newtonsoft.json/index.json",
            None,
        ),
        "/api/v1/package-cache/nuget/flat/{package_id}/{version}/{filename}": (
            "GET",
            f"{BASE}/nuget/flat/newtonsoft.json/13.0.3/newtonsoft.json.13.0.3.nupkg",
            None,
        ),
        "/api/v1/admin/package-cache/warm": (
            "POST",
            "/api/v1/admin/package-cache/warm",
            {"packages": [{"ecosystem": "pypi", "name": "requests"}]},
        ),
        # Where a warm would fetch from (#2486). A READ, and the one route in
        # this namespace that reports the deployment's configured upstreams --
        # so an anonymous caller reaching it would disclose every mirror an
        # operator has declared, which is why it is in the walk below rather
        # than exempted as "just a listing".
        "/api/v1/admin/package-cache/sources": (
            "GET",
            "/api/v1/admin/package-cache/sources",
            None,
        ),
        # List, totals, per-file delete and bulk eviction (#2474). Admin routes,
        # so they refuse without the proxy's Basic challenge -- see the walk below.
        "/api/v1/admin/package-cache": (
            "GET",
            "/api/v1/admin/package-cache",
            None,
        ),
        "/api/v1/admin/package-cache/totals": (
            "GET",
            "/api/v1/admin/package-cache/totals",
            None,
        ),
        "/api/v1/admin/package-cache/{file_id}": (
            "DELETE",
            "/api/v1/admin/package-cache/01a06dfc-9451-798c-bed0-348d71a4ac26",
            None,
        ),
        "/api/v1/admin/package-cache/evict": (
            "POST",
            "/api/v1/admin/package-cache/evict",
            {"ecosystem": "pypi"},
        ),
    }

    def test_every_route_has_a_sample_request(self) -> None:
        """Stops the walk below silently skipping a newly added endpoint.

        Paths are normalised onto the canonical prefix first (#1529): every route
        is served at both `/api/v1` and the deprecated `/api/terrapod/v1`, and
        requiring a duplicate sample for each alias would just be bookkeeping —
        they resolve to the same handler. The alias IS exercised, by
        `test_anonymous_is_rejected_on_both_prefixes` below, because "the same
        handler" is a claim about auth worth checking rather than assuming.
        """
        missing = [r.path for r in self._routes() if canonical_path(r.path) not in self.SAMPLES]
        assert not missing, f"add a sample request for: {missing}"

    @pytest.mark.parametrize("prefix", ["/api/v1", "/api/terrapod/v1"])
    @pytest.mark.parametrize("sample", sorted(SAMPLES.values()))
    async def test_anonymous_is_rejected_on_both_prefixes(self, sample: tuple, prefix: str) -> None:
        """Dual-mounting must not create an unauthenticated way in.

        The alias reaches the same handler, so this should hold by construction —
        which is exactly why it is asserted rather than assumed: an auth
        dependency wired per-mount, or a gate keyed on the literal canonical
        prefix, would open a hole that nothing else would notice.
        """
        method, path, body = sample
        path = path.replace("/api/v1", prefix, 1)
        app = create_app()  # no auth override — the real dependency runs
        # The admin route resolves a user before deciding, which needs a session.
        # The proxy routes refuse before touching the database, so this changes
        # nothing for them.
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        async with await _client(app) as c:
            r = await c.request(method, path, json=body)

        if "/admin/" in path:
            # An admin route answers 401 or 403 rather than the proxy's Basic
            # challenge: it is not a route pip or npm ever calls, so there is
            # nothing to challenge. What matters is that it refuses.
            #
            # Keyed on the route, not on the method: this branch used to be
            # `method != "GET"`, which was right while every admin sample was the
            # warm POST and wrong the moment an admin GET arrived -- it would have
            # been held to pip's `WWW-Authenticate: Basic`. It also caught the
            # galaxy publish POST, which is a proxy route rather than an admin one.
            assert r.status_code in (401, 403)
            return
        if method != "GET":
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


class TestARunnerIsAnsweredWithTheAddressItAskedOn:
    """Tarball links have to be reachable by the client that will fetch them (#1566).

    `external_url` is the operator's answer for a browser or a developer's CLI.
    A runner Job is neither: it runs inside the cluster and talks to the API on
    an in-cluster URL precisely because it may have no route to the external one.
    Handed an `external_url` link it follows it and fails — in local development
    the external host resolves to loopback and the pod gets
    `ECONNREFUSED 127.0.0.1:443`, which is exactly how this was found.
    """

    def _request(self, path="/api/terrapod/v1/package-cache/npm/left-pad"):
        from starlette.datastructures import Headers

        req = MagicMock()
        req.url = MagicMock(path=path, scheme="http")
        req.base_url = "http://terrapod-api:8000/"
        req.headers = Headers({})
        return req

    def _runner(self):
        from terrapod.api.dependencies import AuthenticatedUser

        return AuthenticatedUser(
            email="runner",
            display_name="Runner Job",
            roles=["everyone"],
            provider_name="runner_token",
            auth_method="runner_token",
            run_id="run-1",
        )

    def _person(self):
        from terrapod.api.dependencies import AuthenticatedUser

        return AuthenticatedUser(
            email="a@b.c",
            display_name="A",
            roles=["admin"],
            provider_name="local",
            auth_method="session",
        )

    def test_a_runner_gets_the_in_cluster_base(self):
        from terrapod.api.routers.package_cache import _rewrite_base

        with patch("terrapod.api.routers.package_cache.settings") as st:
            st.external_url = "https://terrapod.example.com"
            got = _rewrite_base(self._request(), self._runner())
        assert got.startswith("http://terrapod-api:8000/")

    def test_everyone_else_still_gets_external_url(self):
        # The change must not move the answer for the callers it was written for.
        from terrapod.api.routers.package_cache import _rewrite_base

        with patch("terrapod.api.routers.package_cache.settings") as st:
            st.external_url = "https://terrapod.example.com"
            got = _rewrite_base(self._request(), self._person())
        assert got.startswith("https://terrapod.example.com/")

    def test_no_user_is_treated_as_not_a_runner(self):
        from terrapod.api.routers.package_cache import _rewrite_base

        with patch("terrapod.api.routers.package_cache.settings") as st:
            st.external_url = "https://terrapod.example.com"
            got = _rewrite_base(self._request(), None)
        assert got.startswith("https://terrapod.example.com/")

    def test_nuget_gets_the_same_treatment(self):
        # The NuGet service index advertises absolute URLs the client follows
        # verbatim, and it was missed when npm's was fixed: a runner following an
        # external_url index got "Connection refused (terrapod.local:443)".
        from terrapod.api.routers.package_cache import _nuget_base

        with patch("terrapod.api.routers.package_cache.settings") as st:
            st.external_url = "https://terrapod.example.com"
            got = _nuget_base(
                self._request("/api/v1/package-cache/nuget/index.json"), self._runner()
            )
        assert got.startswith("http://terrapod-api:8000/")
        assert got.endswith("/package-cache/nuget")

    def test_nuget_still_gives_everyone_else_external_url(self):
        from terrapod.api.routers.package_cache import _nuget_base

        with patch("terrapod.api.routers.package_cache.settings") as st:
            st.external_url = "https://terrapod.example.com"
            got = _nuget_base(
                self._request("/api/v1/package-cache/nuget/index.json"), self._person()
            )
        assert got.startswith("https://terrapod.example.com/")

    def test_galaxy_gets_the_same_treatment(self):
        # The third of three surfaces that hand a client an absolute URL it
        # follows. npm was fixed in #1743 and NuGet in #1747; this one kept its
        # own copy of the logic and so was left behind (#1750), which is the
        # shape of bug that copy invites.
        from terrapod.api.routers.package_cache import _galaxy_base

        with patch("terrapod.api.routers.package_cache.settings") as st:
            st.external_url = "https://terrapod.example.com"
            got = _galaxy_base(
                self._request("/api/terrapod/v1/package-cache/galaxy/v3/collections/a/b/"),
                self._runner(),
            )
        assert got.startswith("http://terrapod-api:8000/")
        assert got.endswith("/package-cache/galaxy")

    def test_galaxy_still_gives_everyone_else_external_url(self):
        from terrapod.api.routers.package_cache import _galaxy_base

        with patch("terrapod.api.routers.package_cache.settings") as st:
            st.external_url = "https://terrapod.example.com"
            got = _galaxy_base(
                self._request("/api/terrapod/v1/package-cache/galaxy/v3/collections/a/b/"),
                self._person(),
            )
        assert got.startswith("https://terrapod.example.com/")

    def test_galaxy_with_no_caller_named_is_unchanged(self):
        # The parameter is optional, so a call site that has not been updated
        # keeps the old behaviour rather than silently becoming a runner answer.
        from terrapod.api.routers.package_cache import _galaxy_base

        with patch("terrapod.api.routers.package_cache.settings") as st:
            st.external_url = "https://terrapod.example.com"
            got = _galaxy_base(
                self._request("/api/terrapod/v1/package-cache/galaxy/v3/collections/a/b/")
            )
        assert got.startswith("https://terrapod.example.com/")

    def test_the_runner_answer_keeps_the_prefix_the_caller_used(self):
        # npm resolves `_authToken` by request path, so an answer on a different
        # prefix would strip the runner's own credential from every fetch.
        from terrapod.api.routers.package_cache import _rewrite_base

        with patch("terrapod.api.routers.package_cache.settings") as st:
            st.external_url = "https://terrapod.example.com"
            legacy = _rewrite_base(self._request(), self._runner())
            canonical = _rewrite_base(
                self._request("/api/v1/package-cache/npm/left-pad"), self._runner()
            )
        assert "/api/terrapod/v1/package-cache/npm" in legacy
        assert "/api/v1/package-cache/npm" in canonical


GALAXY = f"{BASE}/galaxy/v3/collections/community/general"

GALAXY_VERSIONS = {
    "meta": {"count": 1},
    "links": {"first": None, "previous": None, "next": None, "last": None},
    "data": [{"version": "9.0.0", "href": "https://galaxy.ansible.com/x/versions/9.0.0/"}],
}

GALAXY_VERSION = {
    "version": "9.0.0",
    "href": "https://galaxy.ansible.com/api/v3/collections/community/general/versions/9.0.0/",
    "namespace": {"name": "community"},
    "collection": {"name": "general"},
    "artifact": {"filename": "community-general-9.0.0.tar.gz", "sha256": "b" * 64, "size": 1},
    "download_url": "https://galaxy.ansible.com/download/community-general-9.0.0.tar.gz",
    "metadata": {"dependencies": {}},
}


@asynccontextmanager
async def _nothing_published():
    """No collection of this name published here, so the proxy path is taken.

    Mocked a layer below the route rather than at the route itself: the private
    read gate (#2300) asks the registry first, and short-circuiting it would
    leave these tests asserting on a handler that does not run in production.
    """
    with (
        patch(
            "terrapod.services.registry_collection_service.get_collection",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "terrapod.services.registry_collection_service.list_versions",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "terrapod.services.registry_collection_service.get_version",
            new=AsyncMock(return_value=None),
        ),
    ):
        yield


class TestTheCollectionIndexIsReadBackFromCache:
    """#2512: an unsealed node served none of what it stored.

    The document was fetched upstream on every request and then written to a
    copy only a *sealed* node would ever read -- so a warm proxy still paid a
    full upstream round trip (and `fetch_versions` pages through every version
    of the collection) for each of the ~20 sequential metadata calls an install
    of five pinned collections makes. The read below is the fix; these pin that
    it happens, which is the only thing the old write-only shape lacked.

    Driven through the real route rather than against the helper: the defect was
    that the handler never consulted the cache, so a test calling the helper
    itself would have passed throughout.
    """

    @patch("terrapod.api.routers.package_cache.load_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_versions", new_callable=AsyncMock)
    async def test_a_cached_version_list_is_served_without_asking_upstream(
        self, fetch, load
    ) -> None:
        load.return_value = json.dumps(GALAXY_VERSIONS).encode()

        async with _nothing_published():
            async with await _client(_app()) as c:
                r = await c.get(f"{GALAXY}/versions/", headers=AUTH)

        assert r.status_code == 200
        assert [d["version"] for d in r.json()["data"]] == ["9.0.0"]
        # The whole of the issue: upstream was not asked.
        fetch.assert_not_awaited()

    @patch("terrapod.api.routers.package_cache.store_document", new_callable=AsyncMock)
    @patch("terrapod.api.routers.package_cache.load_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_versions", new_callable=AsyncMock)
    async def test_an_expired_version_list_goes_upstream_again(self, fetch, load, store) -> None:
        """An expiry reads as a miss, so the existing cold path answers it."""
        load.return_value = None
        fetch.return_value = GALAXY_VERSIONS

        async with _nothing_published():
            async with await _client(_app()) as c:
                r = await c.get(f"{GALAXY}/versions/", headers=AUTH)

        assert r.status_code == 200
        fetch.assert_awaited_once()
        store.assert_awaited_once()

    @patch("terrapod.api.routers.package_cache.load_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_version", new_callable=AsyncMock)
    async def test_a_cached_version_detail_is_served_without_asking_upstream(
        self, fetch, load
    ) -> None:
        load.return_value = json.dumps(GALAXY_VERSION).encode()

        async with _nothing_published():
            async with await _client(_app()) as c:
                r = await c.get(f"{GALAXY}/versions/9.0.0/", headers=AUTH)

        assert r.status_code == 200
        # Still rewritten on the way out. A cached document is upstream's, so
        # serving it as stored would send the client to galaxy.ansible.com --
        # an install that resolves through Terrapod and downloads from the
        # internet, which looks identical to working until there is no route out.
        assert "galaxy.ansible.com" not in r.text
        fetch.assert_not_awaited()

    @patch("terrapod.api.routers.package_cache.load_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_collection", new_callable=AsyncMock)
    async def test_a_cached_collection_detail_is_served_without_asking_upstream(
        self, fetch, load
    ) -> None:
        load.return_value = json.dumps(
            {
                "href": "https://galaxy.ansible.com/api/v3/collections/community/general/",
                "namespace": "community",
                "name": "general",
                "deprecated": False,
                "versions_url": "https://galaxy.ansible.com/x/versions/",
                "highest_version": {"version": "9.0.0", "href": "https://galaxy.ansible.com/x/"},
            }
        ).encode()

        async with _nothing_published():
            async with await _client(_app()) as c:
                r = await c.get(f"{GALAXY}/", headers=AUTH)

        assert r.status_code == 200
        fetch.assert_not_awaited()


class TestWhichDocumentsMayGoStale:
    """The bound is chosen per document, and getting it backwards is invisible.

    A listing served for ever means a newly published version can never be
    installed. An immutable per-version detail given an expiry means a round
    trip that returns identical bytes -- the cost this issue is about. Neither
    shows in a response body, so the `max_age` the route asks for is asserted.
    """

    @patch("terrapod.api.routers.package_cache.store_document", new_callable=AsyncMock)
    @patch("terrapod.api.routers.package_cache.load_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_versions", new_callable=AsyncMock)
    async def test_the_version_list_is_bounded(self, fetch, load, store) -> None:
        load.return_value = None
        fetch.return_value = GALAXY_VERSIONS

        async with _nothing_published():
            async with await _client(_app()) as c:
                await c.get(f"{GALAXY}/versions/", headers=AUTH)

        assert load.await_args.kwargs["max_age"] == galaxy_service.LISTING_TTL

    @patch("terrapod.api.routers.package_cache.store_document", new_callable=AsyncMock)
    @patch("terrapod.api.routers.package_cache.load_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_version", new_callable=AsyncMock)
    async def test_the_version_detail_is_not_bounded(self, fetch, load, store) -> None:
        load.return_value = None
        fetch.return_value = GALAXY_VERSION

        async with _nothing_published():
            async with await _client(_app()) as c:
                await c.get(f"{GALAXY}/versions/9.0.0/", headers=AUTH)

        assert load.await_args.kwargs["max_age"] is None

    @patch("terrapod.api.routers.package_cache.store_document", new_callable=AsyncMock)
    @patch("terrapod.api.routers.package_cache.load_document", new_callable=AsyncMock)
    @patch("terrapod.services.package_cache.galaxy.fetch_versions", new_callable=AsyncMock)
    async def test_a_cache_read_that_raises_still_serves_the_request(
        self, fetch, load, store
    ) -> None:
        """Reading the cache is best-effort, exactly as writing it already is.

        A database hiccup or a storage blip has an answer upstream can still
        give, so an install degrades to slow rather than failing outright.
        """
        load.side_effect = RuntimeError("database went away mid-request")
        fetch.return_value = GALAXY_VERSIONS

        async with _nothing_published():
            async with await _client(_app()) as c:
                r = await c.get(f"{GALAXY}/versions/", headers=AUTH)

        assert r.status_code == 200
        fetch.assert_awaited_once()


class TestEveryPackageCacheRouteRecordsItsDuration:
    """#2512's second half: uvicorn's access log carries no duration.

    A line is written when a request *completes* and says nothing about how long
    it took, so a slow index lookup and a fast one look the same, and a gap
    between two lines is equally a held request or an idle client. One report
    arrived with a 60-second hole that could have been either and the logs could
    not say which. These pin that the number is recorded.
    """

    def _timings(self, log):
        return [c for c in log.info.call_args_list if c.args[:1] == ("Package cache request",)]

    @patch("terrapod.services.package_cache.pypi.fetch_index")
    async def test_a_successful_request_logs_a_duration(self, fetch) -> None:
        fetch.return_value = PYPI_INDEX

        with patch("terrapod.api.routers.package_cache.logger") as log:
            async with await _client(_app()) as c:
                await c.get(f"{BASE}/pypi/simple/flask/", headers=AUTH)

        timings = self._timings(log)
        assert len(timings) == 1
        kwargs = timings[0].kwargs
        assert kwargs["status"] == 200
        assert kwargs["path"].endswith("/pypi/simple/flask/")
        assert isinstance(kwargs["duration_ms"], float)

    async def test_a_refused_request_logs_one_too(self) -> None:
        """The requests most worth timing are the ones that went wrong."""
        with patch("terrapod.api.routers.package_cache.logger") as log:
            async with await _client(_app()) as c:
                await c.get(f"{BASE}/galaxy/v3/collections/Bad.Name/x/", headers=AUTH)

        timings = self._timings(log)
        assert len(timings) == 1
        assert timings[0].kwargs["status"] == 404

    def test_every_package_cache_router_is_timed(self) -> None:
        """Derived rather than listed, so a seventh ecosystem is covered the day
        it is added -- and `include_router` carries the *source* router's class
        across, so setting it on the aggregate alone would silently drop it for
        every route included into it, which is all of them."""
        from fastapi import APIRouter
        from fastapi.routing import APIRoute

        from terrapod.api.routers import package_cache as mod

        routers = [
            getattr(mod, n)
            for n in dir(mod)
            if n.endswith("_router") and isinstance(getattr(mod, n), APIRouter)
        ]
        assert len(routers) >= 6, routers
        for router in routers:
            assert router.route_class is mod.TimedRoute, router

        built = mod.build_router()
        assert built is not None
        mounted = [r for r in built.routes if isinstance(r, APIRoute)]
        assert mounted, "no routes mounted"
        assert all(isinstance(r, mod.TimedRoute) for r in mounted)
