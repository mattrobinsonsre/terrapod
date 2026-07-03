"""Tests for execution hook CRUD + association endpoints (#619)."""

from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_HOOKS = "/api/terrapod/v1/execution-hooks"


def _user(email="admin@example.com", roles=None):
    return AuthenticatedUser(
        email=email,
        display_name="Admin",
        roles=roles if roles is not None else ["admin"],
        provider_name="local",
        auth_method="session",
    )


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


class TestCreateHook:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_happy(self, *_mocks) -> None:
        app, _db = _make_app(_user())
        body = {
            "data": {
                "attributes": {
                    "name": "hosts-entry",
                    "hook-point": "pre_init",
                    "script": "echo 1.2.3.4 host >> /etc/hosts",
                    "priority": 5,
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(_HOOKS, json=body, headers=_AUTH)
        assert r.status_code == 201
        attrs = r.json()["data"]["attributes"]
        assert attrs["name"] == "hosts-entry"
        assert attrs["hook-point"] == "pre_init"
        assert attrs["priority"] == 5

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_invalid_hook_point_422(self, *_mocks) -> None:
        app, _db = _make_app(_user())
        body = {"data": {"attributes": {"name": "x", "hook-point": "post_destroy"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(_HOOKS, json=body, headers=_AUTH)
        assert r.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_missing_name_422(self, *_mocks) -> None:
        app, _db = _make_app(_user())
        body = {"data": {"attributes": {"hook-point": "pre_init"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(_HOOKS, json=body, headers=_AUTH)
        assert r.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_duplicate_name_409(self, *_mocks) -> None:
        app, db = _make_app(_user())
        db.commit = AsyncMock(side_effect=IntegrityError("dup", None, Exception("unique")))
        body = {"data": {"attributes": {"name": "dup", "hook-point": "pre_init"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(_HOOKS, json=body, headers=_AUTH)
        assert r.status_code == 409

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_non_admin_forbidden(self, *_mocks) -> None:
        app, _db = _make_app(_user(roles=["everyone"]))
        body = {"data": {"attributes": {"name": "x", "hook-point": "pre_init"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(_HOOKS, json=body, headers=_AUTH)
        assert r.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_non_numeric_priority_422(self, *_mocks) -> None:
        # A non-numeric priority must be a 422 (client error), never a 500.
        app, _db = _make_app(_user())
        body = {"data": {"attributes": {"name": "x", "hook-point": "pre_init", "priority": "high"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.post(_HOOKS, json=body, headers=_AUTH)
        assert r.status_code == 422


class TestListHooks:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_list_empty(self, *_mocks) -> None:
        app, db = _make_app(_user())
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.get(_HOOKS, headers=_AUTH)
        assert r.status_code == 200
        assert r.json()["data"] == []

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_list_non_admin_forbidden(self, *_mocks) -> None:
        app, _db = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            r = await c.get(_HOOKS, headers=_AUTH)
        assert r.status_code == 403
