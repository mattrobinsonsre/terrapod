"""Tests for the encryption-at-rest status endpoint (#553)."""

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_URL = "/api/terrapod/v1/admin/encryption"


def _user(roles):
    return AuthenticatedUser(
        email="u@example.com",
        display_name="U",
        roles=roles,
        provider_name="local",
        auth_method="session",
    )


def _app(user):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    return app


@pytest.mark.asyncio
async def test_admin_gets_status_with_decryptable_field():
    app = _app(_user(["admin"]))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=_BASE) as c:
        resp = await c.get(_URL, headers=_AUTH)
    assert resp.status_code == 200
    attrs = resp.json()["data"]["attributes"]
    # Default (encryption disabled) → not enabled but decryptable (nothing at risk).
    assert attrs["enabled"] is False
    assert attrs["decryptable"] is True
    assert "active_version" in attrs and "dek_versions" in attrs


@pytest.mark.asyncio
async def test_non_admin_forbidden():
    app = _app(_user(["everyone"]))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=_BASE) as c:
        resp = await c.get(_URL, headers=_AUTH)
    assert resp.status_code == 403
