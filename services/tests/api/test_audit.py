"""Tests for audit log API endpoint."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(email="admin@example.com", roles=None):
    return AuthenticatedUser(
        email=email,
        display_name="Admin",
        roles=roles or ["admin"],
        provider_name="local",
        auth_method="session",
    )


def _make_app(user=None, mock_db=None):
    app = create_app()
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


def _mock_audit_entry(
    entry_id=None,
    actor_email="user@example.com",
    action="GET",
    resource_type="workspaces",
    resource_id="ws-123",
    status_code=200,
    duration_ms=42,
):
    entry = MagicMock()
    entry.id = entry_id or uuid.uuid4()
    entry.timestamp = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
    entry.actor_email = actor_email
    entry.actor_ip = "127.0.0.1"
    entry.action = action
    entry.resource_type = resource_type
    entry.resource_id = resource_id
    entry.status_code = status_code
    entry.request_id = "req-abc"
    entry.duration_ms = duration_ms
    entry.detail = ""
    return entry


class TestAuditLogRequiresAuth:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_no_auth_returns_401(self, *mocks):
        """GET /api/v2/admin/audit-log without auth → 401."""
        app = create_app()
        # codeql[py/unnecessary-lambda]
        app.dependency_overrides[get_db] = lambda: AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/admin/audit-log")
        assert resp.status_code == 401

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_non_admin_returns_403(self, *mocks):
        """Regular user (no admin/audit role) → 403."""
        user = _user(roles=["everyone"])
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/admin/audit-log", headers=_AUTH)
        assert resp.status_code == 403


class TestAuditLogReturnsEntries:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.audit.query_audit_log")
    async def test_returns_json_api_shape(self, mock_query, *mocks):
        """Happy path: returns JSON:API data + meta.pagination."""
        entry = _mock_audit_entry()
        mock_query.return_value = ([entry], 1)

        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/admin/audit-log", headers=_AUTH)

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["data"]) == 1
        assert body["data"][0]["type"] == "audit-log-entries"
        assert body["data"][0]["attributes"]["actor-email"] == "user@example.com"
        assert body["data"][0]["attributes"]["status-code"] == 200
        assert body["meta"]["pagination"]["total-count"] == 1

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.audit.query_audit_log")
    async def test_audit_role_can_access(self, mock_query, *mocks):
        """Audit role (non-admin) → 200."""
        mock_query.return_value = ([], 0)
        user = _user(roles=["audit", "everyone"])
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/admin/audit-log", headers=_AUTH)
        assert resp.status_code == 200


class TestAuditLogPassesFilters:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.audit.query_audit_log")
    async def test_filter_params_forwarded(self, mock_query, *mocks):
        """Filter query params are forwarded to the service."""
        mock_query.return_value = ([], 0)
        app, _ = _make_app(_user())

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                "/api/v2/admin/audit-log",
                params={
                    "filter[actor]": "admin@test.com",
                    "filter[resource-type]": "runs",
                    "filter[action]": "POST",
                    "page[number]": "2",
                    "page[size]": "10",
                },
                headers=_AUTH,
            )
        assert resp.status_code == 200
        mock_query.assert_called_once()
        kwargs = mock_query.call_args
        assert kwargs[1]["actor"] == "admin@test.com"
        assert kwargs[1]["resource_type"] == "runs"
        assert kwargs[1]["action"] == "POST"
        assert kwargs[1]["page_number"] == 2
        assert kwargs[1]["page_size"] == 10


class TestAuditLogPagination:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.audit.query_audit_log")
    async def test_pagination_metadata(self, mock_query, *mocks):
        """Pagination meta reflects total and page info."""
        entries = [_mock_audit_entry() for _ in range(5)]
        mock_query.return_value = (entries, 25)

        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                "/api/v2/admin/audit-log",
                params={"page[number]": "2", "page[size]": "5"},
                headers=_AUTH,
            )
        assert resp.status_code == 200
        meta = resp.json()["meta"]["pagination"]
        assert meta["current-page"] == 2
        assert meta["page-size"] == 5
        assert meta["total-count"] == 25
        assert meta["total-pages"] == 5

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.audit.query_audit_log")
    async def test_empty_result(self, mock_query, *mocks):
        """Empty result → empty data, total-pages 0."""
        mock_query.return_value = ([], 0)
        app, _ = _make_app(_user())

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/admin/audit-log", headers=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == []
        assert body["meta"]["pagination"]["total-pages"] == 0
