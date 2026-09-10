"""Native workspace read, update and list serve every enabled engine (#1554).

A Pulumi workspace could be created on the native surface (#1535) but read,
listed and edited only on the TFE one, which is Terraform-only by design — so it
was create-only. These routes are the fix. What is pinned here, with the database
mocked: a non-Terraform workspace is served by id and by name, the lookup is
scoped to the engines this deployment enables (so a gated-off engine is absent),
RBAC is the same as the TFE surface's, and the update body is the shared one.
The SQL itself is exercised against real Postgres in
tests/integration/test_native_workspace_routes.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.auth.capabilities import caps_for_level
from tests.api.test_workspaces import _AUTH, _BASE, _make_app, _mock_workspace, _user

NATIVE = "terrapod.api.routers.workspace_extensions"
TFE = "terrapod.api.routers.tfe_v2"
BOTH = ("pulumi", "terraform")


def _pulumi(**kw):
    ws = _mock_workspace(name="proj::dev", **kw)
    ws.engine = "pulumi"
    return ws


def _db(ws) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = ws
    result.scalars.return_value.all.return_value = [ws] if ws is not None else []
    db.execute.return_value = result
    db.add = MagicMock()
    return db


def _params(db, call: int = 0) -> dict:
    """The bound parameters of one executed statement."""
    return db.execute.await_args_list[call].args[0].compile().params


async def _call(method: str, path: str, db, caps, **kw):
    app, _ = _make_app(_user(), db)
    with (
        patch("terrapod.engines.known_engines", return_value=kw.pop("engines", BOTH)),
        patch(f"{NATIVE}.resolve_workspace_capabilities_for", AsyncMock(return_value=caps)),
        patch(f"{TFE}.resolve_workspace_capabilities_for", AsyncMock(return_value=caps)),
        patch(f"{TFE}._resolve_live_pools", AsyncMock(return_value=frozenset())),
        patch("terrapod.redis.client.publish_workspace_event", AsyncMock()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            return await c.request(method, path, headers=_AUTH, **kw)


class TestRead:
    async def test_a_pulumi_workspace_is_served_by_id(self):
        ws = _pulumi()
        db = _db(ws)
        r = await _call("GET", f"/api/v1/workspaces/ws-{ws.id}", db, caps_for_level("read"))
        assert r.status_code == 200, r.text
        assert r.json()["data"]["attributes"]["engine"] == "pulumi"
        assert ws.id in _params(db).values()

    async def test_and_by_its_project_stack_name(self):
        """How a person meets a Pulumi workspace, so it has to resolve."""
        db = _db(_pulumi())
        r = await _call("GET", "/api/v1/workspaces/proj::dev", db, caps_for_level("read"))
        assert r.status_code == 200, r.text
        assert "proj::dev" in _params(db).values()

    async def test_the_lookup_is_scoped_to_the_enabled_engines(self):
        """Off means absent: with Pulumi gated off the query cannot match a
        Pulumi row, and the caller gets the same 404 as for no workspace at all."""
        db = _db(None)
        r = await _call(
            "GET",
            "/api/v1/workspaces/proj::dev",
            db,
            caps_for_level("read"),
            engines=("terraform",),
        )
        assert r.status_code == 404
        assert ["terraform"] in [
            list(v) for v in _params(db).values() if isinstance(v, (list, tuple))
        ]

    async def test_no_read_access_is_404_not_403(self):
        """A lookup by name must not reveal which names exist."""
        r = await _call("GET", "/api/v1/workspaces/proj::dev", _db(_pulumi()), frozenset())
        assert r.status_code == 404


class TestUpdate:
    async def test_a_pulumi_workspace_can_be_edited(self):
        ws = _pulumi()
        db = _db(ws)
        r = await _call(
            "PATCH",
            f"/api/v1/workspaces/ws-{ws.id}",
            db,
            caps_for_level("admin"),
            json={"data": {"type": "workspaces", "attributes": {"pulumi-bind-plan": True}}},
        )
        assert r.status_code == 200, r.text
        assert ws.pulumi_bind_plan is True
        db.commit.assert_awaited()

    async def test_the_shared_body_still_validates(self):
        """The same rules as the TFE surface — not a looser copy."""
        ws = _mock_workspace()
        r = await _call(
            "PATCH",
            f"/api/v1/workspaces/ws-{ws.id}",
            _db(ws),
            caps_for_level("admin"),
            json={"data": {"attributes": {"pulumi-bind-plan": True}}},
        )
        assert r.status_code == 422

    async def test_read_without_settings_is_403(self):
        ws = _pulumi()
        db = _db(ws)
        r = await _call(
            "PATCH",
            f"/api/v1/workspaces/ws-{ws.id}",
            db,
            caps_for_level("read"),
            json={"data": {"attributes": {"pulumi-bind-plan": True}}},
        )
        assert r.status_code == 403
        db.commit.assert_not_awaited()

    async def test_no_access_at_all_is_404(self):
        r = await _call(
            "PATCH",
            "/api/v1/workspaces/proj::dev",
            _db(_pulumi()),
            frozenset(),
            json={"data": {"attributes": {}}},
        )
        assert r.status_code == 404


class TestList:
    async def test_lists_every_enabled_engine(self):
        db = _db(_pulumi())
        r = await _call("GET", "/api/v1/workspaces", db, caps_for_level("read"))
        assert r.status_code == 200, r.text
        assert [d["attributes"]["engine"] for d in r.json()["data"]] == ["pulumi"]
        assert list(BOTH) in [list(v) for v in _params(db).values() if isinstance(v, (list, tuple))]

    async def test_an_engine_filter_narrows_it(self):
        db = _db(_pulumi())
        r = await _call(
            "GET", "/api/v1/workspaces?filter[engine]=pulumi", db, caps_for_level("read")
        )
        assert r.status_code == 200, r.text
        assert "pulumi" in _params(db).values()

    async def test_unreadable_workspaces_are_left_out(self):
        r = await _call("GET", "/api/v1/workspaces", _db(_pulumi()), frozenset())
        assert r.status_code == 200
        assert r.json()["data"] == []
