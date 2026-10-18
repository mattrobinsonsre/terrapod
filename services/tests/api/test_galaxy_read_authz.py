"""Reading a PRIVATE Ansible collection requires read on that collection.

Publishing has been gated since #1949; reading was not. A collection scoped with
labels — the mechanism `RegistryCollection` carries `labels` and `owner_email`
for, and the one the write path already honours — was served to any principal who
could authenticate anywhere on the deployment, including a runner token belonging
to an unrelated workspace's run. The module registry authorises its own CLI
download path (`registry_modules.download_module_cli`), so this was an asymmetry
rather than a decision.

Driven through the real routes with the real capability resolver, mocking only the
layer *below* the new check — the collection and version lookups. Mocking the
check itself would leave these tests asserting on a function that does not run in
production, which is the failure mode the guard exists to catch.

The runner-token case is the other half and matters as much: a run must still be
able to install a collection, and that permission is inherited from the registry
axis's read floor rather than restated here. A guard that only proved strangers
are refused would teach the over-restriction.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers.package_cache import authenticate_package_request
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.storage import get_storage

NS, NAME, VER = "victim", "widgets", "1.0.0"
BASE = f"/api/v1/package-cache/galaxy/v3/collections/{NS}/{NAME}"
DETAIL = f"{BASE}/"
VERSIONS = f"{BASE}/versions/"
VERSION = f"{BASE}/versions/{VER}/"
DOWNLOAD = f"{BASE}/versions/{VER}/download/{NS}-{NAME}-{VER}.tar.gz"

OWNER = "owner@example.com"


@pytest.fixture(autouse=True)
def _galaxy_on():
    """Pin the capability on so a default change cannot mute these (#1986)."""
    before = settings.registry.package_cache.galaxy.enabled
    settings.registry.package_cache.galaxy.enabled = True
    yield
    settings.registry.package_cache.galaxy.enabled = before


def _user(email: str = "stranger@example.com", roles=None, auth_method: str = "session"):
    return AuthenticatedUser(
        email=email,
        display_name=None,
        roles=list(roles or ["everyone"]),
        provider_name="local",
        auth_method=auth_method,
    )


def _runner():
    """A run's own token, which must be able to install collections."""
    return AuthenticatedUser(
        email="runner",
        display_name="Runner Job",
        roles=["everyone"],
        provider_name="runner_token",
        auth_method="runner_token",
        run_id=uuid.uuid4(),
        run_phase="apply",
    )


def _collection(owner: str = OWNER, labels=None):
    row = MagicMock()
    row.namespace, row.name = NS, NAME
    row.owner_email = owner
    row.labels = labels or {"team": "platform"}
    return row


def _version():
    row = MagicMock()
    row.version = VER
    row.artifact_sha256 = "a" * 64
    row.size = 1024
    row.manifest = {"dependencies": {}, "tags": [], "description": ""}
    row.signature = None
    row.signing_key_id = None
    row.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    row.collection_id = uuid.uuid4()
    return row


def _app(user: AuthenticatedUser):
    app = create_app()
    app.dependency_overrides[authenticate_package_request] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_storage] = lambda: AsyncMock()
    return app


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _private(get_coll, list_versions=None, get_version=None):
    """Make the deployment hold this collection privately."""
    get_coll.return_value = _collection()
    if list_versions is not None:
        list_versions.return_value = [_version()]
    if get_version is not None:
        get_version.return_value = _version()


class TestAStrangerCannotReadAPrivateCollection:
    """The finding itself, on every route that serves private content."""

    @patch("terrapod.services.registry_collection_service.list_versions", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    async def test_collection_detail(self, get_coll, list_versions) -> None:
        _private(get_coll, list_versions=list_versions)
        async with await _client(_app(_user())) as c:
            r = await c.get(DETAIL)
        assert r.status_code == 404, r.text

    @patch("terrapod.services.registry_collection_service.list_versions", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    async def test_version_list(self, get_coll, list_versions) -> None:
        _private(get_coll, list_versions=list_versions)
        async with await _client(_app(_user())) as c:
            r = await c.get(VERSIONS)
        assert r.status_code == 404, r.text

    @patch("terrapod.services.registry_collection_service.get_version", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    async def test_version_detail(self, get_coll, get_version) -> None:
        _private(get_coll, get_version=get_version)
        async with await _client(_app(_user())) as c:
            r = await c.get(VERSION)
        assert r.status_code == 404, r.text

    @patch("terrapod.api.routers.package_cache._redirect_to_object", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_version", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    async def test_the_artifact_itself(self, get_coll, get_version, redirect) -> None:
        """The one that would actually hand over the content."""
        _private(get_coll, get_version=get_version)
        async with await _client(_app(_user())) as c:
            r = await c.get(DOWNLOAD)
        assert r.status_code == 404, r.text
        redirect.assert_not_awaited()

    @patch(
        "terrapod.services.registry_collection_service.get_collection_of_version",
        new_callable=AsyncMock,
    )
    @patch(
        "terrapod.services.registry_collection_service.get_version_by_id", new_callable=AsyncMock
    )
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    async def test_the_import_poll(self, get_coll, by_id, of_version) -> None:
        """Keyed on an opaque id, so a weak probe — but it is still a read of a
        private collection's row, and leaving one of five sites open is how the
        next person concludes the class is handled."""
        get_coll.return_value = _collection()
        by_id.return_value = _version()
        of_version.return_value = _collection()
        async with await _client(_app(_user())) as c:
            r = await c.get(f"/api/v1/package-cache/galaxy/v3/imports/collections/{uuid.uuid4()}/")
        assert r.status_code == 404, r.text


class TestARefusedReadDoesNotFallThroughToUpstream:
    """Serving upstream's collection of the same name would hand the caller
    different content under the name they asked for — the shadowing hazard the
    handler already refuses in the other direction."""

    @patch("terrapod.services.package_cache.galaxy.fetch_collection", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.list_versions", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    async def test_nothing_is_fetched_upstream(self, get_coll, list_versions, fetch) -> None:
        _private(get_coll, list_versions=list_versions)
        fetch.return_value = {"namespace": NS, "name": NAME}

        async with await _client(_app(_user())) as c:
            r = await c.get(DETAIL)

        assert r.status_code == 404, r.text
        fetch.assert_not_awaited()


class TestWhoMayRead:
    """The permitted side. Without these the guard teaches over-restriction."""

    @patch("terrapod.services.registry_collection_service.list_versions", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    async def test_the_owner_may(self, get_coll, list_versions) -> None:
        _private(get_coll, list_versions=list_versions)
        async with await _client(_app(_user(email=OWNER))) as c:
            r = await c.get(DETAIL)
        assert r.status_code == 200, r.text
        assert r.json()["name"] == NAME

    @patch("terrapod.services.registry_collection_service.list_versions", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    async def test_a_platform_admin_may(self, get_coll, list_versions) -> None:
        _private(get_coll, list_versions=list_versions)
        async with await _client(_app(_user(roles=["admin"]))) as c:
            r = await c.get(DETAIL)
        assert r.status_code == 200, r.text

    @patch("terrapod.services.registry_collection_service.list_versions", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    async def test_a_runner_token_may(self, get_coll, list_versions) -> None:
        """#1931's own acceptance: a runner must install collections during a run,
        the same implicit read the module and provider registries grant. It comes
        from the registry axis's read floor, not from a second rule here."""
        _private(get_coll, list_versions=list_versions)
        async with await _client(_app(_runner())) as c:
            r = await c.get(DETAIL)
        assert r.status_code == 200, r.text

    @patch("terrapod.api.routers.package_cache._redirect_to_object", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_version", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    async def test_a_runner_token_gets_the_artifact(self, get_coll, get_version, redirect) -> None:
        """The install only works if the bytes come too."""
        _private(get_coll, get_version=get_version)
        redirect.return_value = MagicMock(status_code=307)
        async with await _client(_app(_runner())) as c:
            await c.get(DOWNLOAD)
        redirect.assert_awaited_once()


class TestAnUnpublishedCollectionIsUnaffected:
    """A name nobody published here has nothing to authorise against, and the
    proxy path must still be reached — otherwise the gate breaks every upstream
    install, which is the whole feature."""

    @patch("terrapod.services.package_cache.galaxy.fetch_collection", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.list_versions", new_callable=AsyncMock)
    @patch("terrapod.services.registry_collection_service.get_collection", new_callable=AsyncMock)
    async def test_a_stranger_still_reaches_the_proxy(self, get_coll, list_versions, fetch) -> None:
        get_coll.return_value = None
        list_versions.return_value = []
        fetch.return_value = {"namespace": NS, "name": NAME, "deprecated": False}

        async with await _client(_app(_user())) as c:
            r = await c.get(DETAIL)

        assert r.status_code == 200, r.text
        fetch.assert_awaited_once()


class TestEveryPrivateReadSiteAuthorises:
    """Source-introspection invariant: a route that reads private collection
    content must authorise it.

    This is the gate the behavioural tests above cannot be. The reads landed in
    #1482 and the write gate in #1949, so the defect was not a wrong check but a
    *missing* one on a path nobody revisited — and a new read route added later
    would reproduce it with every behavioural test still green.

    The subject is derived from the router's own source rather than listed here,
    so a sixth read site is in scope automatically. The ordering requirement is
    derived too: a reader addressed by `namespace`/`name` can be authorised
    before it runs, so it must be; the import poll is addressed by an opaque
    version id and has to read the row to learn which collection to authorise
    against, so for that shape the requirement is presence, with the refusal
    still ahead of any response.
    """

    #: Functions in `registry_collection_service` that return private content.
    #: `get_collection` is deliberately absent: it is how the authorisers
    #: themselves look the row up, so requiring a guard around it is circular.
    READERS = {"list_versions", "get_version", "get_version_by_id"}

    GUARD = "_require_collection_read"

    def _router_functions(self):
        import ast
        import pathlib

        from terrapod.api.routers import package_cache

        tree = ast.parse(pathlib.Path(package_cache.__file__).read_text(encoding="utf-8"))
        return [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)]

    def _read_calls(self, fn):
        """Every `collections.<reader>(...)` call inside one function."""
        import ast

        out = []
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in self.READERS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "collections"
            ):
                out.append(node)
        return out

    def _guard_lines(self, fn):
        import ast

        return [
            node.lineno
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == self.GUARD
        ]

    def test_every_reader_is_guarded(self) -> None:
        import ast

        checked = 0
        offenders = []
        for fn in self._router_functions():
            calls = self._read_calls(fn)
            if not calls:
                continue
            guards = self._guard_lines(fn)
            for call in calls:
                checked += 1
                # Does this reader name the collection, or only an opaque id?
                named = any(
                    isinstance(a, ast.Name) and a.id == "namespace"
                    for a in call.args + [k.value for k in call.keywords]
                )
                if not guards:
                    offenders.append(f"{fn.name}:{call.lineno} has no {self.GUARD}")
                elif named and not any(g < call.lineno for g in guards):
                    offenders.append(
                        f"{fn.name}:{call.lineno} reads private content before {self.GUARD}"
                    )

        assert offenders == [], "unguarded private collection reads: " + "; ".join(offenders)
        # A renamed helper must fail here rather than match nothing and pass.
        # Proven to bind: raising this to 6 fails with "only inspected 5".
        assert checked >= 5, f"the gate only inspected {checked} read sites; it has gone blind"
