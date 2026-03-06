"""Tests for notification configuration CRUD endpoints."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(email="test@example.com", roles=None):
    return AuthenticatedUser(
        email=email,
        display_name="Test",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _mock_workspace(ws_id=None, name="test-ws"):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = name
    ws.auto_apply = False
    ws.execution_backend = "tofu"
    ws.labels = {}
    ws.owner_email = "test@example.com"
    return ws


def _mock_nc(nc_id=None, ws=None, name="test-notif", dest_type="generic"):
    nc = MagicMock()
    nc.id = nc_id or uuid.uuid4()
    ws = ws or _mock_workspace()
    nc.workspace_id = ws.id
    nc.workspace = ws
    nc.name = name
    nc.destination_type = dest_type
    nc.url = "https://example.com/hook"
    nc.token = None
    nc.enabled = False
    nc.triggers = ["run:completed"]
    nc.email_addresses = []
    nc.delivery_responses = []
    nc.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    nc.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return nc


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


# ── Create ────────────────────────────────────────────────────────────


class TestCreateNotificationConfiguration:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.notification_configurations.resolve_workspace_permission")
    async def test_create_generic(self, mock_resolve, *mocks):
        """Create generic webhook → 201."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/notification-configurations",
                json={
                    "data": {
                        "type": "notification-configurations",
                        "attributes": {
                            "name": "My Webhook",
                            "destination-type": "generic",
                            "url": "https://example.com/hook",
                            "triggers": ["run:completed", "run:errored"],
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201
        assert resp.json()["data"]["type"] == "notification-configurations"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.notification_configurations.resolve_workspace_permission")
    async def test_create_invalid_type(self, mock_resolve, *mocks):
        """Invalid destination-type → 422."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/notification-configurations",
                json={
                    "data": {
                        "type": "notification-configurations",
                        "attributes": {
                            "name": "Bad",
                            "destination-type": "sms",
                            "triggers": [],
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.notification_configurations.resolve_workspace_permission")
    async def test_create_invalid_triggers(self, mock_resolve, *mocks):
        """Invalid trigger event → 422."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/notification-configurations",
                json={
                    "data": {
                        "type": "notification-configurations",
                        "attributes": {
                            "name": "Bad Trigger",
                            "destination-type": "generic",
                            "url": "https://example.com",
                            "triggers": ["run:invalid"],
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "Invalid triggers" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.notification_configurations.resolve_workspace_permission")
    async def test_create_requires_admin(self, mock_resolve, *mocks):
        """Write permission → 403."""
        mock_resolve.return_value = "write"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/notification-configurations",
                json={
                    "data": {
                        "type": "notification-configurations",
                        "attributes": {
                            "name": "Test",
                            "destination-type": "generic",
                            "url": "https://example.com",
                            "triggers": [],
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.notification_configurations.resolve_workspace_permission")
    async def test_create_email_requires_addresses(self, mock_resolve, *mocks):
        """Email type without email-addresses → 422."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/notification-configurations",
                json={
                    "data": {
                        "type": "notification-configurations",
                        "attributes": {
                            "name": "Email Test",
                            "destination-type": "email",
                            "triggers": ["run:completed"],
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "email-addresses" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.notification_configurations.resolve_workspace_permission")
    async def test_create_generic_requires_url(self, mock_resolve, *mocks):
        """Generic without url → 422."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/notification-configurations",
                json={
                    "data": {
                        "type": "notification-configurations",
                        "attributes": {
                            "name": "No URL",
                            "destination-type": "generic",
                            "triggers": [],
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422


# ── List ──────────────────────────────────────────────────────────────


class TestListNotificationConfigurations:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.notification_configurations.resolve_workspace_permission")
    async def test_list(self, mock_resolve, *mocks):
        """List returns configs. Read permission sufficient."""
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        nc = _mock_nc(ws=ws)

        app, mock_db = _make_app(_user())
        mock_result_ws = MagicMock()
        mock_result_ws.scalar_one_or_none.return_value = ws
        mock_result_ncs = MagicMock()
        mock_result_ncs.scalars.return_value.all.return_value = [nc]

        mock_db.execute.side_effect = [mock_result_ws, mock_result_ncs]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/workspaces/ws-{ws.id}/notification-configurations",
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1
        assert resp.json()["data"][0]["type"] == "notification-configurations"


# ── Show ──────────────────────────────────────────────────────────────


class TestShowNotificationConfiguration:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.notification_configurations.resolve_workspace_permission")
    async def test_show(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        nc = _mock_nc()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = nc
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/notification-configurations/nc-{nc.id}",
                headers=_AUTH,
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["attributes"]["name"] == "test-notif"
        # Token should never be returned
        assert "token" not in data["attributes"]
        assert "has-token" in data["attributes"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_show_not_found(self, *mocks):
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/notification-configurations/nc-{uuid.uuid4()}",
                headers=_AUTH,
            )
        assert resp.status_code == 404


# ── Update ────────────────────────────────────────────────────────────


class TestUpdateNotificationConfiguration:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.notification_configurations.resolve_workspace_permission")
    async def test_update_name(self, mock_resolve, *mocks):
        """PATCH updates name → 200."""
        mock_resolve.return_value = "admin"
        nc = _mock_nc()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = nc
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/notification-configurations/nc-{nc.id}",
                json={
                    "data": {
                        "type": "notification-configurations",
                        "attributes": {"name": "Updated Name"},
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.notification_configurations.resolve_workspace_permission")
    async def test_update_requires_admin(self, mock_resolve, *mocks):
        """Write permission → 403."""
        mock_resolve.return_value = "write"
        nc = _mock_nc()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = nc
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/notification-configurations/nc-{nc.id}",
                json={"data": {"type": "notification-configurations", "attributes": {"name": "X"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403


# ── Delete ────────────────────────────────────────────────────────────


class TestDeleteNotificationConfiguration:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.notification_configurations.resolve_workspace_permission")
    async def test_delete(self, mock_resolve, *mocks):
        """Admin can delete → 204."""
        mock_resolve.return_value = "admin"
        nc = _mock_nc()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = nc
        mock_db.execute.return_value = mock_result
        mock_db.delete = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/v2/notification-configurations/nc-{nc.id}",
                headers=_AUTH,
            )
        assert resp.status_code == 204


# ── Verify ────────────────────────────────────────────────────────────


class TestVerifyNotificationConfiguration:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.notification_configurations.resolve_workspace_permission")
    @patch("terrapod.api.routers.notification_configurations.deliver_notification")
    @patch("terrapod.api.routers.notification_configurations.record_delivery_response")
    async def test_verify(self, mock_record, mock_deliver, mock_resolve, *mocks):
        """Verify sends test notification and records response."""
        mock_resolve.return_value = "admin"
        mock_deliver.return_value = {"status": 200, "body": "ok", "success": True}

        nc = _mock_nc()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = nc
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/notification-configurations/nc-{nc.id}/actions/verify",
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["success"] is True
        mock_deliver.assert_called_once()
        mock_record.assert_called_once()
