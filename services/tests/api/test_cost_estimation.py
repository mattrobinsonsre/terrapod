"""Tests for the cost-estimation pricesheet router (#871).

Covers the runner-facing download redirect (auth + disabled + not-cached
branches) and the admin-gated status/refresh surfaces.
"""

from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.storage import get_storage

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_DL = "/api/terrapod/v1/cost-estimation/pricesheet"
_STATUS = "/api/terrapod/v1/cost-estimation/pricesheet/status"
_REFRESH = "/api/terrapod/v1/cost-estimation/pricesheet/refresh"
_WS_COST = "/api/terrapod/v1/workspaces/ws-abc123/cost-estimate"
_SVC = "terrapod.services.cost_pricesheet_service"
_WSVC = "terrapod.services.workspace_cost_service"


def _user(roles=None):
    return AuthenticatedUser(
        email="admin@example.com",
        display_name="Admin",
        roles=roles or ["admin"],
        provider_name="local",
        auth_method="session",
    )


def _make_app(user=None):
    app = create_app()
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[get_storage] = lambda: AsyncMock()
    return app


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestDownloadPricesheet:
    async def test_no_auth_returns_401(self, *mocks):
        app = create_app()
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[get_storage] = lambda: AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(_DL)
        assert resp.status_code == 401

    async def test_disabled_returns_404(self, *mocks):
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            with patch.object(settings.cost_estimation, "enabled", False):
                resp = await c.get(_DL, headers=_AUTH)
        assert resp.status_code == 404

    @patch(f"{_SVC}.ensure_pricesheet", new_callable=AsyncMock)
    async def test_not_cached_returns_404(self, mock_ensure, *mocks):
        mock_ensure.return_value = False  # fetch failed and nothing cached
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            with patch.object(settings.cost_estimation, "enabled", True):
                resp = await c.get(_DL, headers=_AUTH)
        assert resp.status_code == 404

    @patch(f"{_SVC}.pricesheet_download_url", new_callable=AsyncMock)
    @patch(f"{_SVC}.ensure_pricesheet", new_callable=AsyncMock)
    async def test_happy_path_redirects(self, mock_ensure, mock_url, *mocks):
        mock_ensure.return_value = True  # pull-through returns a usable sheet
        mock_url.return_value = "https://example.test/presigned/prices.csv"
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE, follow_redirects=False
        ) as c:
            with patch.object(settings.cost_estimation, "enabled", True):
                resp = await c.get(_DL, headers=_AUTH)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.test/presigned/prices.csv"


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestAdminSurfaces:
    @patch(f"{_SVC}.pricesheet_available", new_callable=AsyncMock)
    async def test_status_admin(self, mock_avail, *mocks):
        mock_avail.return_value = True
        app = _make_app(_user(roles=["admin"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            with patch.object(settings.cost_estimation, "enabled", True):
                resp = await c.get(_STATUS, headers=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True and body["available"] is True
        assert "Terrapod" in body["source"]

    async def test_status_non_admin_forbidden(self, *mocks):
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(_STATUS, headers=_AUTH)
        assert resp.status_code == 403

    @patch(f"{_SVC}.refresh_pricesheet", new_callable=AsyncMock)
    async def test_refresh_admin_happy(self, mock_refresh, *mocks):
        mock_refresh.return_value = 12345
        app = _make_app(_user(roles=["admin"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            with patch.object(settings.cost_estimation, "enabled", True):
                resp = await c.post(_REFRESH, headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json() == {"refreshed": True, "size_bytes": 12345}

    @patch(f"{_SVC}.refresh_pricesheet", new_callable=AsyncMock)
    async def test_refresh_upstream_failure_502(self, mock_refresh, *mocks):
        mock_refresh.side_effect = RuntimeError("upstream down")
        app = _make_app(_user(roles=["admin"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            with patch.object(settings.cost_estimation, "enabled", True):
                resp = await c.post(_REFRESH, headers=_AUTH)
        assert resp.status_code == 502


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestWorkspaceCostEstimate:
    """The workspace state-cost endpoint (#871) — the RBAC + engine work lives
    in workspace_cost_service (unit-tested separately); the router is thin."""

    async def test_disabled_returns_404(self, *mocks):
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            with patch.object(settings.cost_estimation, "enabled", False):
                resp = await c.get(_WS_COST, headers=_AUTH)
        assert resp.status_code == 404

    @patch(f"{_WSVC}.estimate_workspace_cost", new_callable=AsyncMock)
    async def test_happy_path(self, mock_estimate, *mocks):
        mock_estimate.return_value = {
            "currency": "USD",
            "total": {"min": 292.0, "max": 292.0},
            "previous": {"min": 292.0, "max": 292.0},
            "diff": {"min": 0.0, "max": 0.0},
            "resources": [
                {
                    "address": "aws_instance.web",
                    "type": "aws_instance",
                    "name": "web",
                    "change": "noop",
                    "monthly": {"min": 73.0, "max": 73.0},
                }
            ],
            "unpriced": [],
            "state-version": {"id": "sv-xyz", "serial": 7, "created-at": "2026-07-20T09:00:00Z"},
        }
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            with patch.object(settings.cost_estimation, "enabled", True):
                resp = await c.get(_WS_COST, headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["type"] == "workspace-cost-estimates"
        assert data["id"] == "workspace-cost-abc123"
        assert data["relationships"]["workspace"]["data"]["id"] == "ws-abc123"
        assert data["attributes"]["total"]["max"] == 292.0
        assert data["attributes"]["state-version"]["serial"] == 7

    @patch(f"{_WSVC}.estimate_workspace_cost", new_callable=AsyncMock)
    async def test_no_state_returns_zeroed_estimate(self, mock_estimate, *mocks):
        mock_estimate.return_value = {
            "currency": "USD",
            "total": {"min": 0.0, "max": 0.0},
            "previous": {"min": 0.0, "max": 0.0},
            "diff": {"min": 0.0, "max": 0.0},
            "resources": [],
            "unpriced": [],
            "state-version": None,
        }
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            with patch.object(settings.cost_estimation, "enabled", True):
                resp = await c.get(_WS_COST, headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["state-version"] is None

    @patch(f"{_WSVC}.estimate_workspace_cost", new_callable=AsyncMock)
    async def test_pricesheet_unavailable_returns_503(self, mock_estimate, *mocks):
        from terrapod.services.workspace_cost_service import PricesheetUnavailable

        mock_estimate.side_effect = PricesheetUnavailable("no sheet")
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            with patch.object(settings.cost_estimation, "enabled", True):
                resp = await c.get(_WS_COST, headers=_AUTH)
        assert resp.status_code == 503
