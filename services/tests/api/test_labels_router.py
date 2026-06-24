"""Tests for the read-only labels-browser router.

The wrapper utility `validate_labels` is covered separately in
test_labels_wrapper.py; this file covers the three browse endpoints
(`/labels`, `/labels/{key}`, `/labels/{key}/{value}`) — auth gate plus
the happy paths that forward to labels_service and pass the
RBAC-bearing user through.
"""

from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(roles=None):
    return AuthenticatedUser(
        email="user@example.com",
        display_name="User",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _make_app(user=None):
    app = create_app()
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


class TestAuth:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_no_auth_401(self, *mocks):
        app = create_app()
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/labels")
        assert resp.status_code == 401


class TestListKeys:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.labels_service.aggregate_keys", new_callable=AsyncMock)
    async def test_happy(self, mock_agg, *mocks):
        mock_agg.return_value = [
            {"key": "team", "value-count": 3, "counts": {"workspaces": 5}},
        ]
        user = _user()
        app = _make_app(user)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/labels", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"][0]["key"] == "team"
        # The RBAC-bearing user is forwarded to the service.
        assert mock_agg.call_args[0][1] is user


class TestListValues:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.labels_service.aggregate_values_for_key", new_callable=AsyncMock)
    async def test_happy(self, mock_agg, *mocks):
        mock_agg.return_value = [{"value": "platform", "counts": {"workspaces": 2}}]
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/labels/team", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"][0]["value"] == "platform"
        assert mock_agg.call_args[0][2] == "team"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.labels_service.aggregate_values_for_key", new_callable=AsyncMock)
    async def test_empty_is_valid(self, mock_agg, *mocks):
        mock_agg.return_value = []
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/labels/unknown", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"] == []


class TestListEntities:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.labels_service.list_entities_for_label", new_callable=AsyncMock)
    async def test_happy(self, mock_list, *mocks):
        mock_list.return_value = {
            "workspaces": [{"id": "ws-1", "name": "prod"}],
        }
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/labels/team/platform", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["workspaces"][0]["id"] == "ws-1"
        # key + value forwarded positionally after (db, user).
        assert mock_list.call_args[0][2] == "team"
        assert mock_list.call_args[0][3] == "platform"
