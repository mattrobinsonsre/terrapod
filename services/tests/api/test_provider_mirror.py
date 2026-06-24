"""Services-API auth-gate tests for the provider network mirror
(`routers/provider_mirror.py`).

CLAUDE.md pins this as a HARD fact: the `{version}.json` / `index.json` mirror
endpoints are AUTHENTICATED (the returned download URLs are not). The caching
*service* is unit-tested, but nothing asserted the *router* declares the auth
dependency — so dropping `Depends(get_current_user)` would pass every existing
test. These prove the gate is wired: the override only fires if the route
actually depends on `get_current_user`.
"""

from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user

_BASE = "http://test"
_INDEX = "/v1/providers/registry.terraform.io/hashicorp/null/index.json"
_VERSION = "/v1/providers/registry.terraform.io/hashicorp/null/3.2.1.json"


def _deny() -> AuthenticatedUser:
    raise HTTPException(status_code=401, detail="unauthorized")


@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestProviderMirrorAuthGate:
    async def test_index_declares_auth_dependency(self, *_mocks):
        app = create_app()
        app.dependency_overrides[get_current_user] = _deny  # only fires if declared
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(_INDEX)
        assert resp.status_code == 401

    async def test_version_declares_auth_dependency(self, *_mocks):
        app = create_app()
        app.dependency_overrides[get_current_user] = _deny
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(_VERSION)
        assert resp.status_code == 401
