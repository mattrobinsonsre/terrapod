"""Tests for the terraform/tofu binary-cache + provider-cache router.

Covers the runner-facing download redirect, the admin list/warm/purge
surfaces (admin-gated), and the upstream-version suggestion endpoint —
happy paths plus auth/RBAC and the disabled-cache + upstream-failure
error branches.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.storage import get_storage

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}
_DL = "/api/terrapod/v1/binary-cache/terraform/1.9.0/linux/amd64"


def _user(email="admin@example.com", roles=None):
    return AuthenticatedUser(
        email=email,
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


def _cached_binary():
    e = MagicMock()
    e.id = uuid.uuid4()
    e.tool = "terraform"
    e.version = "1.9.0"
    e.os = "linux"
    e.arch = "amd64"
    e.shasum = "abc123"
    e.download_url = "https://example.test/terraform_1.9.0.zip"
    e.cached_at = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
    return e


def _cached_provider():
    e = MagicMock()
    e.id = uuid.uuid4()
    e.hostname = "registry.terraform.io"
    e.namespace = "hashicorp"
    e.type = "aws"
    e.version = "5.0.0"
    e.os = "linux"
    e.arch = "amd64"
    e.shasum = "deadbeef"
    e.cached_at = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
    return e


class TestDownloadBinary:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_no_auth_returns_401(self, *mocks):
        app = create_app()
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        app.dependency_overrides[get_storage] = lambda: AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(_DL)
        assert resp.status_code == 401

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.binary_cache.get_or_cache_binary", new_callable=AsyncMock)
    @patch("terrapod.api.routers.binary_cache.resolve_version", new_callable=AsyncMock)
    async def test_happy_path_redirects(self, mock_resolve, mock_get, *mocks):
        mock_resolve.return_value = "1.9.0"
        mock_get.return_value = "https://example.test/terraform_1.9.0.zip"
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=_BASE, follow_redirects=False
        ) as c:
            resp = await c.get(_DL, headers=_AUTH)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.test/terraform_1.9.0.zip"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_disabled_cache_returns_404(self, *mocks):
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            with patch.object(settings.registry.binary_cache, "enabled", False):
                resp = await c.get(_DL, headers=_AUTH)
        assert resp.status_code == 404

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.binary_cache.resolve_version", new_callable=AsyncMock)
    async def test_value_error_returns_400(self, mock_resolve, *mocks):
        mock_resolve.side_effect = ValueError("unknown version")
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(_DL, headers=_AUTH)
        assert resp.status_code == 400

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.binary_cache.get_or_cache_binary", new_callable=AsyncMock)
    @patch("terrapod.api.routers.binary_cache.resolve_version", new_callable=AsyncMock)
    async def test_upstream_failure_returns_502(self, mock_resolve, mock_get, *mocks):
        mock_resolve.return_value = "1.9.0"
        mock_get.side_effect = RuntimeError("upstream 500")
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(_DL, headers=_AUTH)
        assert resp.status_code == 502


class TestAvailableVersions:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.binary_cache.list_available_versions", new_callable=AsyncMock)
    async def test_happy_path(self, mock_list, *mocks):
        mock_list.return_value = ["1.9.0", "1.8.5"]
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/binary-cache/versions?tool=tofu", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"] == ["1.9.0", "1.8.5"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.binary_cache.list_available_versions", new_callable=AsyncMock)
    async def test_bad_tool_returns_400(self, mock_list, *mocks):
        mock_list.side_effect = ValueError("unknown tool")
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/binary-cache/versions?tool=nope", headers=_AUTH)
        assert resp.status_code == 400


class TestAdminListBinaries:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_non_admin_returns_403(self, *mocks):
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/admin/binary-cache", headers=_AUTH)
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.binary_cache.list_cached_binaries", new_callable=AsyncMock)
    async def test_admin_lists_jsonapi(self, mock_list, *mocks):
        mock_list.return_value = [_cached_binary()]
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/admin/binary-cache", headers=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"][0]["type"] == "cached-binaries"
        assert body["data"][0]["attributes"]["tool"] == "terraform"
        assert body["data"][0]["attributes"]["download-url"].endswith(".zip")


class TestAdminWarm:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_non_admin_returns_403(self, *mocks):
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/admin/binary-cache/warm",
                json={"tool": "terraform", "version": "1.9.0"},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.binary_cache.warm_binary", new_callable=AsyncMock)
    async def test_admin_warm_happy(self, mock_warm, *mocks):
        mock_warm.return_value = "https://example.test/terraform_1.9.0.zip"
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/admin/binary-cache/warm",
                json={"tool": "terraform", "version": "1.9.0"},
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "cached"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.binary_cache.warm_binary", new_callable=AsyncMock)
    async def test_admin_warm_value_error_returns_400(self, mock_warm, *mocks):
        mock_warm.side_effect = ValueError("bad version")
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/admin/binary-cache/warm",
                json={"tool": "terraform", "version": "nope"},
                headers=_AUTH,
            )
        assert resp.status_code == 400


class TestAdminPurge:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.binary_cache.purge_binary", new_callable=AsyncMock)
    async def test_admin_purge_binary(self, mock_purge, *mocks):
        mock_purge.return_value = 2
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                "/api/terrapod/v1/admin/binary-cache/terraform/1.9.0", headers=_AUTH
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "purged", "count": 2}

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_purge_binary_non_admin_403(self, *mocks):
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                "/api/terrapod/v1/admin/binary-cache/terraform/1.9.0", headers=_AUTH
            )
        assert resp.status_code == 403


class TestProviderCacheAdmin:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.binary_cache.list_cached_providers", new_callable=AsyncMock)
    async def test_admin_list_providers(self, mock_list, *mocks):
        mock_list.return_value = [_cached_provider()]
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/admin/provider-cache", headers=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"][0]["type"] == "cached-providers"
        assert body["data"][0]["attributes"]["provider-type"] == "aws"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_list_providers_non_admin_403(self, *mocks):
        app = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/admin/provider-cache", headers=_AUTH)
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.binary_cache.purge_cached_provider", new_callable=AsyncMock)
    async def test_admin_purge_provider(self, mock_purge, *mocks):
        mock_purge.return_value = 3
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                "/api/terrapod/v1/admin/provider-cache/registry.terraform.io/hashicorp/aws/5.0.0",
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "purged", "count": 3}
