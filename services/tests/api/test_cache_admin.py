"""The admin routes for the package cache and the VCS archive cache (#2474).

Driven as REQUESTS throughout, never by calling the service. The service has its
own tests in tests/services/test_cache_inventory.py and every one of them passes
with these routes deleted — which is the distinction that matters for a surface
whose whole point is that an operator can reach it.

What this file owns that the service tests cannot: the admin gate on all eight
routes, the shape each one puts on the wire, and the two pieces of `meta` that
are load-bearing rather than decorative — the archive listing's cursor and the
archive totals' `truncated`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.storage import get_storage

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}

#: Every route this file covers, with the method it answers. Derived subjects
#: beat a list, but there is nothing to derive from for "which routes exist" —
#: so this IS the list, and the admin-gate test iterates it rather than naming
#: eight routes eight times. A route added without a row here is a route with no
#: gate test, which `test_every_route_in_this_file_is_covered` refuses.
ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/api/v1/admin/package-cache"),
    ("GET", "/api/v1/admin/package-cache/totals"),
    ("DELETE", "/api/v1/admin/package-cache/f1"),
    ("POST", "/api/v1/admin/package-cache/evict"),
    ("GET", "/api/v1/admin/vcs-archive-cache"),
    ("GET", "/api/v1/admin/vcs-archive-cache/totals"),
    ("DELETE", "/api/v1/admin/vcs-archive-cache?key=vcs_archives/c/o/r/a-full.tar.gz"),
    ("POST", "/api/v1/admin/vcs-archive-cache/evict"),
)


def _user(email="admin@example.com", roles=None):
    return AuthenticatedUser(
        email=email,
        display_name="Admin",
        roles=roles if roles is not None else ["admin"],
        provider_name="local",
        auth_method="session",
    )


@dataclass
class _Obj:
    """An `ObjectMeta` stand-in, spelled as the protocol spells it."""

    key: str
    size_bytes: int = 0
    last_modified: datetime | None = None


class _Storage:
    def __init__(self, objects=()):
        self.objects = sorted(objects, key=lambda o: o.key)
        self.deleted: list[str] = []

    async def list_prefix(self, prefix: str, *, after: str = "", limit: int | None = None):
        out = [o for o in self.objects if o.key.startswith(prefix) and o.key > after]
        return out if limit is None else out[:limit]

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


def _file(**kw):
    row = MagicMock()
    row.id = kw.get("id", "f1")
    row.ecosystem = kw.get("ecosystem", "galaxy")
    row.name = kw.get("name", "community.general")
    row.version = kw.get("version", "9.2.0")
    row.filename = kw.get("filename", "community-general-9.2.0.tar.gz")
    row.storage_key = kw.get("storage_key", "package_cache/galaxy/community.general/x.tar.gz")
    row.size = kw.get("size", 1234)
    row.digest = kw.get("digest", "sha256:abc")
    row.cached_at = kw.get("cached_at", datetime(2026, 11, 1, 9, 0, tzinfo=UTC))
    row.last_accessed_at = kw.get("last_accessed_at", datetime(2026, 11, 9, 9, 0, tzinfo=UTC))
    return row


def _make_app(user=None, rows=(), storage=None, get_row=None):
    app = create_app()
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user

    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(rows)
    # package_totals reads the result as an iterable of rows with named columns.
    result.__iter__ = lambda self: iter([])
    db.execute.return_value = result
    db.get = AsyncMock(return_value=get_row)
    db.delete = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db

    store = storage if storage is not None else _Storage()
    app.dependency_overrides[get_storage] = lambda: store
    return app, db, store


async def _call(app, method: str, path: str, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        return await c.request(method, path, **kw)


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestTheAdminGate:
    """All eight routes, both directions, iterated rather than written out.

    Written as a loop over `ROUTES` deliberately: eight hand-written gate tests
    is where the ninth route silently arrives without one, which is the shape
    this project has recorded repeatedly.
    """

    @pytest.mark.parametrize("method,path", ROUTES)
    async def test_no_auth_is_401(self, _db, _redis, _storage, method, path):
        app, _, _ = _make_app(user=None)
        resp = await _call(app, method, path, json={})
        assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"

    @pytest.mark.parametrize("method,path", ROUTES)
    async def test_a_non_admin_is_403(self, _db, _redis, _storage, method, path):
        app, _, _ = _make_app(user=_user(roles=["everyone"]))
        resp = await _call(app, method, path, json={})
        assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"

    def test_every_route_in_this_file_is_covered(self, *_m):
        """The list above must BE the router, not a subset of it.

        Otherwise a ninth route arrives with no gate test and the two loops
        above keep passing — the gate still green while the surface has grown
        past it.
        """
        from terrapod.api.routers import cache_admin

        # BOTH routers. The module exports two -- the package half is mounted
        # only while its capability serves -- so reading one of them would have
        # halved this gate's subject the moment they were split, and halved it
        # silently, which is the failure this test exists to be.
        live = [
            (sorted(r.methods)[0], r.path)
            for router in (cache_admin.package_router, cache_admin.router)
            for r in router.routes
            if getattr(r, "methods", None)
        ]
        assert len(live) >= 8, (
            f"only {len(live)} routes read off the router; the accessor has changed "
            "rather than the router having shrunk"
        )

        # ROUTES carries CONCRETE paths, because the gate tests issue real
        # requests; the router carries TEMPLATES. So each live template is turned
        # into a pattern and matched, rather than compared as a string -- which
        # would report `/{file_id}` as uncovered by `/f1` and read as a missing
        # gate test.
        covered = [(m, p.split("?")[0].removeprefix("/api/v1")) for m, p in ROUTES]
        missing = []
        for method, template in live:
            pattern = re.compile(
                "^"
                + re.sub(
                    r"\{[^}]+\}",
                    "[^/]+",
                    re.escape(template).replace(r"\{", "{").replace(r"\}", "}"),
                )
                + "$"
            )
            if not any(m == method and pattern.match(path) for m, path in covered):
                missing.append((method, template))
        assert not missing, (
            f"{len(missing)} route(s) in cache_admin have no admin-gate row in ROUTES: "
            f"{sorted(missing)}"
        )


class TestTheCapabilityGate:
    """The package half goes with its capability; the archive half does not.

    The convention is that a switched-off capability's surface is ABSENT rather
    than answering 404 -- `scripts/airgap-gate.sh --gated-off` asserts it against
    a running stack, and `oci_admin` is the precedent. These four routes were
    mounted unconditionally when they were written, which left the
    `/admin/package-cache/` namespace half-present: these answering and the
    pre-existing `warm` route, on the same prefix, gone.

    Asserted here as well as in the live gate because the live gate is an
    expensive place to learn it, and because the live gate's matcher is
    `'/package-cache/' in path` -- which does not even see `GET
    /admin/package-cache`, the listing.
    """

    def _paths(self) -> list[str]:
        from terrapod.api.app import create_application

        return [getattr(r, "path", "") for r in create_application().routes]

    #: The paths this module owns, derived from its own router so a fifth cannot
    #: arrive outside this test's reach.
    def _owned(self) -> set[str]:
        from terrapod.api.routers import cache_admin

        return {r.path for r in cache_admin.package_router.routes if getattr(r, "methods", None)}

    def test_the_package_routes_are_absent_when_the_cache_is_off(self, monkeypatch):
        from terrapod.config import settings

        monkeypatch.setattr(settings.registry.package_cache, "enabled", False)
        mounted = set(self._paths())
        # Matched on the OWNED set rather than on the substring `package-cache`,
        # because one route on that prefix is not ours and legitimately survives:
        # `POST /admin/package-cache/warm` lives on the shared warm router, which
        # stays mounted while the container registry is on so that
        # `/admin/warm-jobs/{id}` remains reachable, and refuses per ecosystem
        # with a 404 instead. Asserting the bare substring here would fail on a
        # correct tree -- and would be a test about somebody else's router.
        still_there = sorted(path for path in self._owned() if path in mounted)
        assert still_there == [], f"still mounted with the capability off: {still_there}"
        # ...and the bare listing specifically, because the live gate's own
        # matcher (`'/package-cache/' in path`) cannot see a path that ENDS at
        # the cache, so that one route would pass it either way.
        assert "/api/v1/admin/package-cache" not in mounted

    def test_the_archive_routes_stay_when_the_cache_is_off(self, monkeypatch):
        """Without this half, unmounting everything would pass the test above."""
        from terrapod.config import settings

        monkeypatch.setattr(settings.registry.package_cache, "enabled", False)
        assert [p for p in self._paths() if "vcs-archive-cache" in p] != []

    def test_both_halves_are_there_when_the_cache_is_on(self, monkeypatch):
        from terrapod.config import settings

        monkeypatch.setattr(settings.registry.package_cache, "enabled", True)
        paths = self._paths()
        assert [p for p in paths if "admin/package-cache" in p] != []
        assert [p for p in paths if "vcs-archive-cache" in p] != []


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestThePackageCacheRoutes:
    async def test_the_listing_puts_every_attribute_on_the_wire(self, *_m):
        app, _, _ = _make_app(_user(), rows=[_file()])
        resp = await _call(app, "GET", "/api/v1/admin/package-cache")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data"][0]["type"] == "cached-package-files"
        a = body["data"][0]["attributes"]
        assert a == {
            "ecosystem": "galaxy",
            "name": "community.general",
            "version": "9.2.0",
            "filename": "community-general-9.2.0.tar.gz",
            "size": 1234,
            "digest": "sha256:abc",
            "cached-at": "2026-11-01T09:00:00Z",
            "last-accessed-at": "2026-11-09T09:00:00Z",
        }, "the wire shape changed; a consumer reads these names"
        assert "pagination" in body["meta"], (
            "the package listing is row-backed and ordinary, so it carries the shared "
            "pagination block — unlike the archive listing"
        )

    async def test_the_filters_reach_the_query(self, *_m):
        app, db, _ = _make_app(_user(), rows=[])
        resp = await _call(app, "GET", "/api/v1/admin/package-cache?ecosystem=npm&name=left-")
        assert resp.status_code == 200, resp.text
        clause = str(db.execute.await_args_list[0].args[0].whereclause)
        assert "ecosystem" in clause and "like" in clause.lower(), (
            f"a filter was dropped between the request and the query: {clause}"
        )

    async def test_deleting_a_missing_artifact_is_404_and_commits_nothing(self, *_m):
        app, db, _ = _make_app(_user(), get_row=None)
        resp = await _call(app, "DELETE", "/api/v1/admin/package-cache/nope")
        assert resp.status_code == 404, resp.text
        db.commit.assert_not_awaited()

    async def test_deleting_an_artifact_removes_the_object_and_commits(self, *_m):
        row = _file()
        storage = _Storage()
        app, db, store = _make_app(_user(), get_row=row, storage=storage)
        resp = await _call(app, "DELETE", "/api/v1/admin/package-cache/f1")
        assert resp.status_code == 200, resp.text
        assert store.deleted == [row.storage_key]
        db.commit.assert_awaited()

    async def test_an_unknown_evict_field_is_422(self, *_m):
        """`extra="forbid"`, and on this endpoint that is the difference between
        clearing one ecosystem and clearing the cache: a misspelled filter that
        were ignored would widen the eviction past what the caller asked for."""
        app, db, _ = _make_app(_user())
        resp = await _call(
            app,
            "POST",
            "/api/v1/admin/package-cache/evict",
            json={"ecosytem": "npm"},  # deliberately misspelled
        )
        assert resp.status_code == 422, resp.text
        db.commit.assert_not_awaited()

    async def test_a_negative_age_is_422(self, *_m):
        app, _, _ = _make_app(_user())
        resp = await _call(
            app, "POST", "/api/v1/admin/package-cache/evict", json={"older_than_days": -1}
        )
        assert resp.status_code == 422, resp.text

    async def test_an_eviction_reports_what_it_removed(self, *_m):
        app, db, store = _make_app(_user(), rows=[_file(id="a", size=100), _file(id="b", size=5)])
        resp = await _call(
            app, "POST", "/api/v1/admin/package-cache/evict", json={"ecosystem": "galaxy"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "status": "evicted",
            "files": 2,
            "bytes": 105,
            "sealed": False,
        }
        db.commit.assert_awaited()


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestTheArchiveCacheRoutes:
    async def test_the_listing_puts_every_attribute_on_the_wire(self, *_m):
        obj = _Obj(
            "vcs_archives/conn1/acme/infra/abc123-full.tar.gz",
            4096,
            datetime(2026, 11, 5, 8, 0, tzinfo=UTC),
        )
        app, _, _ = _make_app(_user(), storage=_Storage([obj]))
        resp = await _call(app, "GET", "/api/v1/admin/vcs-archive-cache")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data"][0]["type"] == "cached-vcs-archives"
        assert body["data"][0]["attributes"] == {
            "key": "vcs_archives/conn1/acme/infra/abc123-full.tar.gz",
            "connection-id": "conn1",
            "owner": "acme",
            "repo": "infra",
            "sha": "abc123",
            "paths-hash": "full",
            "size": 4096,
            "last-modified": "2026-11-05T08:00:00Z",
        }

    async def test_the_listing_carries_a_CURSOR_and_not_pagination(self, *_m):
        """The deliberate asymmetry, asserted in both directions.

        There are no rows behind an archive, so a page comes from a storage walk
        and `list_prefix` is key-ordered precisely so a cursor is meaningful. A
        `meta.pagination` block here would invite a client to page by number,
        which would re-walk the whole prefix per page — so its ABSENCE is part
        of the contract rather than an omission.
        """
        objs = [_Obj(f"vcs_archives/c/o/r/s{i:02d}-full.tar.gz", 10) for i in range(5)]
        app, _, _ = _make_app(_user(), storage=_Storage(objs))
        resp = await _call(app, "GET", "/api/v1/admin/vcs-archive-cache?limit=2")
        assert resp.status_code == 200, resp.text
        meta = resp.json()["meta"]
        assert meta["page-size"] == 2
        assert meta["next-cursor"] == "vcs_archives/c/o/r/s01-full.tar.gz"
        assert "pagination" not in meta, (
            "this listing must not advertise page-number pagination it cannot honour"
        )

    async def test_the_cursor_round_trips_through_the_query_string(self, *_m):
        objs = [_Obj(f"vcs_archives/c/o/r/s{i:02d}-full.tar.gz", 10) for i in range(5)]
        app, _, _ = _make_app(_user(), storage=_Storage(objs))
        first = await _call(app, "GET", "/api/v1/admin/vcs-archive-cache?limit=2")
        cursor = first.json()["meta"]["next-cursor"]
        second = await _call(app, "GET", f"/api/v1/admin/vcs-archive-cache?limit=2&after={cursor}")
        assert second.status_code == 200, second.text
        keys = [e["id"] for e in first.json()["data"]] + [e["id"] for e in second.json()["data"]]
        assert len(keys) == len(set(keys)), "the cursor did not reach the walk; entries repeated"

    async def test_the_last_page_hands_back_an_empty_cursor(self, *_m):
        objs = [_Obj("vcs_archives/c/o/r/a-full.tar.gz", 1)]
        app, _, _ = _make_app(_user(), storage=_Storage(objs))
        resp = await _call(app, "GET", "/api/v1/admin/vcs-archive-cache?limit=10")
        assert resp.json()["meta"]["next-cursor"] == ""

    async def test_the_totals_report_TRUNCATED_and_the_ceiling(self, *_m):
        """The honest half of a figure that cannot be a total.

        A number smaller than the problem, handed to an operator looking for
        what is filling a bucket, is the one outcome worth preventing — so the
        flag and the ceiling both reach the wire.
        """
        from terrapod.services import cache_inventory as ci

        objs = [_Obj(f"vcs_archives/c/o/r/s{i}-full.tar.gz", 100) for i in range(4)]
        app, _, _ = _make_app(_user(), storage=_Storage(objs))

        with patch.object(ci, "MAX_ARCHIVE_SCAN", 2):
            resp = await _call(app, "GET", "/api/v1/admin/vcs-archive-cache/totals")

        assert resp.status_code == 200, resp.text
        meta = resp.json()["meta"]
        assert meta["truncated"] is True
        assert meta["scan-limit"] == 2
        assert meta["bytes"] == 200, "the figure must be what was actually walked"

    async def test_the_totals_are_not_truncated_when_the_walk_completes(self, *_m):
        objs = [_Obj("vcs_archives/c/acme/infra/a-full.tar.gz", 100)]
        app, _, _ = _make_app(_user(), storage=_Storage(objs))
        resp = await _call(app, "GET", "/api/v1/admin/vcs-archive-cache/totals")
        body = resp.json()
        assert body["meta"]["truncated"] is False
        assert body["meta"] == {
            "archives": 1,
            "bytes": 100,
            "truncated": False,
            "scan-limit": body["meta"]["scan-limit"],
            # Reported on the TOTALS as well as on the evictions, because a
            # confirmation needs it before it asks rather than after.
            "sealed": False,
        }
        assert body["data"][0]["attributes"]["repo"] == "infra"

    @pytest.mark.parametrize(
        "key",
        [
            "state_versions/ws-1/4.tfstate",
            "plan_artifacts/run-1/plan.json",
            "vcs_archives/../state_versions/ws-1/4.tfstate",
        ],
    )
    async def test_deleting_a_key_outside_the_prefix_is_422_and_deletes_nothing(
        self, _db, _redis, _storage, key
    ):
        """This route takes a storage key from a request.

        Without the prefix check an admin typo — or anything that reached an
        admin token — could delete a state version or a plan artifact through an
        endpoint whose name says "cache". The second assertion is the one that
        matters: a guard that refused and deleted anyway would read as working.
        """
        app, _, store = _make_app(_user(), storage=_Storage())
        resp = await _call(app, "DELETE", f"/api/v1/admin/vcs-archive-cache?key={key}")
        assert resp.status_code == 422, resp.text
        assert store.deleted == [], "a refused key still reached storage.delete"

    async def test_deleting_an_archive_key_works(self, *_m):
        key = "vcs_archives/c/o/r/a-full.tar.gz"
        app, _, store = _make_app(_user(), storage=_Storage([_Obj(key, 1)]))
        resp = await _call(app, "DELETE", f"/api/v1/admin/vcs-archive-cache?key={key}")
        assert resp.status_code == 200, resp.text
        assert store.deleted == [key]

    async def test_a_missing_key_parameter_is_422(self, *_m):
        app, _, _ = _make_app(_user())
        resp = await _call(app, "DELETE", "/api/v1/admin/vcs-archive-cache")
        assert resp.status_code == 422, resp.text

    async def test_an_eviction_reports_archives_rather_than_files(self, *_m):
        """The two evictions deliberately name their own unit — `archives` here,
        `files` for packages — because a UI showing "3 files" for three repo
        tarballs would read as three packages."""
        objs = [_Obj(f"vcs_archives/c/o/r/s{i}-full.tar.gz", 50) for i in range(3)]
        app, _, store = _make_app(_user(), storage=_Storage(objs))
        resp = await _call(app, "POST", "/api/v1/admin/vcs-archive-cache/evict", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "status": "evicted",
            "archives": 3,
            "bytes": 150,
            "sealed": False,
        }

    async def test_an_unknown_evict_field_is_422(self, *_m):
        app, _, _ = _make_app(_user())
        resp = await _call(
            app, "POST", "/api/v1/admin/vcs-archive-cache/evict", json={"connection": "c1"}
        )
        assert resp.status_code == 422, resp.text


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestTheDeprecatedAlias:
    """Both prefixes serve these, because `include_terrapod` mounts them twice.

    Asserted rather than assumed: mounting a router at the canonical prefix
    directly is the recorded way the alias silently goes missing, and a lagging
    consumer reads that as a removal.
    """

    async def test_the_alias_serves_the_package_listing(self, *_m):
        app, _, _ = _make_app(_user(), rows=[_file()])
        resp = await _call(app, "GET", "/api/terrapod/v1/admin/package-cache")
        assert resp.status_code == 200, resp.text

    async def test_the_alias_serves_the_archive_listing(self, *_m):
        app, _, _ = _make_app(_user(), storage=_Storage())
        resp = await _call(app, "GET", "/api/terrapod/v1/admin/vcs-archive-cache")
        assert resp.status_code == 200, resp.text


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestASealedNodeIsWarnedAboutRatherThanRefused:
    """On a sealed node an eviction is a DELETION (#2474).

    `artifact_retention_service` skips these caches entirely when
    `registry.cache_only` is set, with the reason in its own comment: "an
    un-refetchable artifact would be lost permanently". These routes are the
    manual path to the same deletion.

    It warns rather than refusing, and the contrast with the WARM route is the
    whole argument: warming is an upstream fetch by definition, so a sealed node
    genuinely cannot do it and a 409 is the truthful answer. Eviction can be
    done, and refusing it would leave an operator with no way to remove a bad
    artifact from exactly the deployment where a bad artifact is hardest to work
    around.

    Found by the agent writing the docs for these routes, not by a test — which
    is itself worth recording: the sweep's guard and the warm route's 409 were
    both in the tree, and nothing compared them with the new routes.
    """

    @pytest.mark.parametrize(
        "method,path,body",
        [
            ("POST", "/api/v1/admin/package-cache/evict", {}),
            ("POST", "/api/v1/admin/vcs-archive-cache/evict", {}),
        ],
    )
    async def test_a_sealed_node_warns_and_still_evicts(
        self, _db, _redis, _storage, method, path, body
    ):
        app, _, _ = _make_app(_user(), rows=[_file()], storage=_Storage())
        with patch.object(settings.registry, "cache_only", True):
            resp = await _call(app, method, path, json=body)

        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["sealed"] is True
        assert "warning" in payload, (
            "a sealed node evicted without saying the bytes cannot come back"
        )
        assert "cache_only" in payload["warning"], (
            "the warning must name the setting, or an operator cannot find what caused it"
        )
        assert payload["status"] == "evicted", (
            "it must still evict -- refusing would leave a sealed node unable to remove "
            "a bad artifact, which is the one deployment where that matters most"
        )

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/admin/package-cache/f1",
            "/api/v1/admin/vcs-archive-cache?key=vcs_archives/c/o/r/a-full.tar.gz",
        ],
    )
    async def test_the_single_deletes_carry_it_too(self, _db, _redis, _storage, path):
        """Both of them, because one artifact is the likelier manual act on a
        sealed node and so the likelier place to be surprised."""
        key = "vcs_archives/c/o/r/a-full.tar.gz"
        app, _, _ = _make_app(_user(), get_row=_file(), storage=_Storage([_Obj(key, 1)]))
        with patch.object(settings.registry, "cache_only", True):
            resp = await _call(app, "DELETE", path)
        assert resp.status_code == 200, resp.text
        assert resp.json()["sealed"] is True

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/admin/package-cache/totals",
            "/api/v1/admin/vcs-archive-cache/totals",
        ],
    )
    async def test_the_TOTALS_report_it_too_so_a_confirmation_can_say_so_first(
        self, _db, _redis, _storage, path
    ):
        """The eviction responses report it AFTER the fact. A client needs it
        BEFORE it asks — a confirmation that cannot say "these bytes will not
        come back" is the one place this fact matters most, and a panel loads
        the totals anyway."""
        app, _, _ = _make_app(_user(), rows=[], storage=_Storage())
        with patch.object(settings.registry, "cache_only", True):
            resp = await _call(app, "GET", path)
        assert resp.status_code == 200, resp.text
        meta = resp.json()["meta"]
        assert meta["sealed"] is True
        assert "cache_only" in meta["warning"]

    async def test_an_unsealed_node_reports_sealed_FALSE_rather_than_omitting_it(
        self, _db, _redis, _storage
    ):
        """Present and false, not absent.

        A consumer that has to distinguish "not sealed" from "this build does not
        report it" would have to know the server version; a field that is always
        there needs no such knowledge, and a UI can render the warning from the
        payload alone.
        """
        app, _, _ = _make_app(_user(), rows=[], storage=_Storage())
        resp = await _call(app, "POST", "/api/v1/admin/package-cache/evict", json={})
        assert resp.status_code == 200, resp.text
        assert resp.json()["sealed"] is False
        assert "warning" not in resp.json(), (
            "an unsealed node must not carry a warning there is nothing to warn about"
        )
