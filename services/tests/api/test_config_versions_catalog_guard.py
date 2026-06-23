"""Catalog config-managed guardrail (#535) on configuration-version creation.

A catalog-managed workspace runs a server-generated wrapper config — a direct
CV upload would diverge it from its catalog item, so the create endpoint
rejects it with 409 before any permission check or write.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(roles=None):
    return AuthenticatedUser(
        email="test@example.com",
        display_name="Test",
        roles=roles or ["admin"],
        provider_name="local",
        auth_method="session",
    )


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


def _mock_workspace(catalog_item_id=None):
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.catalog_item_id = catalog_item_id
    return ws


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
async def test_cv_create_rejected_on_catalog_ws(*mocks):
    ws = _mock_workspace(catalog_item_id=uuid.uuid4())
    app, mock_db = _make_app(_user())
    result = MagicMock()
    result.scalar_one_or_none.return_value = ws
    mock_db.execute.return_value = result

    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        resp = await c.post(
            f"/api/v2/workspaces/ws-{ws.id}/configuration-versions",
            json={"data": {"attributes": {}}},
            headers=_AUTH,
        )
    assert resp.status_code == 409
    assert "service catalog" in resp.json()["detail"]


@patch("terrapod.api.routers.config_versions.run_service.create_configuration_version")
@patch("terrapod.api.routers.config_versions.resolve_workspace_permission_for")
@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
async def test_cv_create_allowed_on_normal_ws(_db, _redis, _storage, mock_resolve, mock_create):
    ws = _mock_workspace(catalog_item_id=None)
    mock_resolve.return_value = "write"
    cv = MagicMock()
    cv.id = uuid.uuid4()
    cv.workspace_id = ws.id
    cv.status = "pending"
    cv.source = "tfe-api"
    cv.speculative = False
    cv.auto_queue_runs = True
    cv.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    mock_create.return_value = cv

    app, mock_db = _make_app(_user())
    result = MagicMock()
    result.scalar_one_or_none.return_value = ws
    mock_db.execute.return_value = result
    mock_db.refresh = AsyncMock()

    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
        resp = await c.post(
            f"/api/v2/workspaces/ws-{ws.id}/configuration-versions",
            json={"data": {"attributes": {}}},
            headers=_AUTH,
        )
    # Not a 409 — the guardrail let it through to the normal create path.
    assert resp.status_code != 409
