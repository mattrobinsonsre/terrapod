"""Tests for VCS connection CRUD endpoints (admin-only, JSON:API).

Covers the previously-untested router
`terrapod.api.routers.vcs_connections`, including the #315 PATCH
partial-update / credential-preservation surface.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _admin():
    return AuthenticatedUser(
        email="admin@example.com",
        display_name="Admin",
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[require_admin] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


def _mock_conn(
    conn_id=None,
    provider="github",
    name="prod-github",
    server_url="",
    token="-----BEGIN KEY-----",
    github_app_id=12345,
    github_installation_id=98765,
    github_account_login="example",
    github_account_type="Organization",
    status="active",
):
    c = MagicMock()
    c.id = conn_id or uuid.uuid4()
    c.provider = provider
    c.name = name
    c.server_url = server_url
    c.token = token
    c.github_app_id = github_app_id
    c.github_installation_id = github_installation_id
    c.github_account_login = github_account_login
    c.github_account_type = github_account_type
    c.status = status
    c.created_at = datetime(2026, 5, 9, tzinfo=UTC)
    c.updated_at = datetime(2026, 5, 9, tzinfo=UTC)
    return c


def _scalar_result(value):
    """A SQLAlchemy result-like whose scalar_one_or_none() returns `value`."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _list_result(values):
    """A SQLAlchemy result-like whose scalars().all() returns `values`."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    return result


# ── List ─────────────────────────────────────────────────────────────────


class TestListConnections:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_returns_all_connections(self, *_mocks):
        conns = [
            _mock_conn(name="gh-a"),
            _mock_conn(name="gl-b", provider="gitlab", token="glpat-xxx"),
        ]
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_list_result(conns))

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/vcs-connections", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert {d["attributes"]["name"] for d in data} == {"gh-a", "gl-b"}
        # Credentials are never echoed; has-token reflects presence only.
        for d in data:
            assert "token" not in d["attributes"]
            assert "private-key" not in d["attributes"]
            assert d["attributes"]["has-token"] is True


# ── Create ───────────────────────────────────────────────────────────────


class TestCreateConnection:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_201_github_happy(self, *_mocks):
        app, db = _make_app(_admin())
        # Duplicate-installation check → none found.
        db.execute = AsyncMock(return_value=_scalar_result(None))
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        body = {
            "data": {
                "attributes": {
                    "name": "prod-github",
                    "provider": "github",
                    "github-app-id": 12345,
                    "github-installation-id": 98765,
                    "private-key": "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
                    "github-account-login": "example",
                    "github-account-type": "Organization",
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/vcs-connections", json=body, headers=_AUTH)
        assert resp.status_code == 201, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["name"] == "prod-github"
        assert attrs["provider"] == "github"
        assert attrs["has-token"] is True
        assert attrs["github-app-id"] == 12345
        assert attrs["github-installation-id"] == 98765
        # The PEM is never echoed back.
        assert "private-key" not in attrs
        assert "token" not in attrs

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_201_gitlab_happy(self, *_mocks):
        app, db = _make_app(_admin())
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        body = {
            "data": {
                "attributes": {
                    "name": "prod-gitlab",
                    "provider": "gitlab",
                    "token": "glpat-deadbeef",
                    "server-url": "https://gitlab.example.com",
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/vcs-connections", json=body, headers=_AUTH)
        assert resp.status_code == 201, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["name"] == "prod-gitlab"
        assert attrs["provider"] == "gitlab"
        assert attrs["server-url"] == "https://gitlab.example.com"
        assert attrs["has-token"] is True
        # GitHub-specific fields are not present for a gitlab connection.
        assert "github-app-id" not in attrs
        assert "token" not in attrs

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_missing_name(self, *_mocks):
        app, _db = _make_app(_admin())
        body = {"data": {"attributes": {"provider": "gitlab", "token": "x"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/vcs-connections", json=body, headers=_AUTH)
        assert resp.status_code == 422
        assert "name is required" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_unsupported_provider(self, *_mocks):
        app, _db = _make_app(_admin())
        body = {"data": {"attributes": {"name": "x", "provider": "bitbucket", "token": "x"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/vcs-connections", json=body, headers=_AUTH)
        assert resp.status_code == 422
        assert "Unsupported provider" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_github_missing_private_key(self, *_mocks):
        app, _db = _make_app(_admin())
        body = {
            "data": {
                "attributes": {
                    "name": "gh",
                    "provider": "github",
                    "github-app-id": 1,
                    "github-installation-id": 2,
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/vcs-connections", json=body, headers=_AUTH)
        assert resp.status_code == 422
        assert "private-key is required" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_gitlab_missing_token(self, *_mocks):
        app, _db = _make_app(_admin())
        body = {"data": {"attributes": {"name": "gl", "provider": "gitlab"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/vcs-connections", json=body, headers=_AUTH)
        assert resp.status_code == 422
        assert "token is required" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_duplicate_github_installation(self, *_mocks):
        app, db = _make_app(_admin())
        # Duplicate-installation check → an existing connection found.
        db.execute = AsyncMock(return_value=_scalar_result(_mock_conn()))

        body = {
            "data": {
                "attributes": {
                    "name": "dup",
                    "provider": "github",
                    "github-app-id": 1,
                    "github-installation-id": 98765,
                    "private-key": "-----BEGIN KEY-----",
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/vcs-connections", json=body, headers=_AUTH)
        assert resp.status_code == 422
        assert "already connected" in resp.json()["detail"]


# ── Show ─────────────────────────────────────────────────────────────────


class TestShowConnection:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_200_when_exists(self, *_mocks):
        conn = _mock_conn()
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(conn))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/terrapod/v1/vcs-connections/vcs-{conn.id}", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["name"] == "prod-github"
        assert resp.json()["data"]["id"] == f"vcs-{conn.id}"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_404_when_missing(self, *_mocks):
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(None))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/terrapod/v1/vcs-connections/vcs-{uuid.uuid4()}", headers=_AUTH
            )
        assert resp.status_code == 404


# ── Update / PATCH (#315) ────────────────────────────────────────────────


class TestUpdateConnection:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_partial_update_name_server_url_status(self, *_mocks):
        conn = _mock_conn(name="old", server_url="", status="active")
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(conn))
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        body = {
            "data": {
                "attributes": {
                    "name": "new-name",
                    "server-url": "https://github.example.com",
                    "status": "disabled",
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{conn.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        assert conn.name == "new-name"
        assert conn.server_url == "https://github.example.com"
        assert conn.status == "disabled"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_provider_change(self, *_mocks):
        conn = _mock_conn(provider="github")
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(conn))
        body = {"data": {"attributes": {"provider": "gitlab"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{conn.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "immutable" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_invalid_status(self, *_mocks):
        conn = _mock_conn()
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(conn))
        body = {"data": {"attributes": {"status": "paused"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{conn.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "active" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_empty_name(self, *_mocks):
        conn = _mock_conn(name="keep")
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(conn))
        body = {"data": {"attributes": {"name": "   "}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{conn.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "cannot be empty" in resp.json()["detail"]
        assert conn.name == "keep"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_credential_omitted_preserves_stored_token(self, *_mocks):
        """No private-key in the body ⇒ stored token untouched and
        has-token stays true (the #315 write-only credential contract)."""
        conn = _mock_conn(provider="github", token="STORED-PEM")
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(conn))
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        body = {"data": {"attributes": {"name": "renamed"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{conn.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        assert conn.token == "STORED-PEM"
        assert resp.json()["data"]["attributes"]["has-token"] is True

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_empty_credential_does_not_rotate(self, *_mocks):
        """An explicitly empty private-key must NOT wipe the stored
        credential — only a non-empty value rotates it."""
        conn = _mock_conn(provider="github", token="STORED-PEM")
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(conn))
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        body = {"data": {"attributes": {"private-key": ""}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{conn.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        assert conn.token == "STORED-PEM"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_non_empty_private_key_rotates(self, *_mocks):
        conn = _mock_conn(provider="github", token="OLD-PEM")
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(conn))
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        body = {"data": {"attributes": {"private-key": "NEW-PEM"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{conn.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        assert conn.token == "NEW-PEM"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_non_empty_gitlab_token_rotates(self, *_mocks):
        conn = _mock_conn(provider="gitlab", token="old-pat")
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(conn))
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        body = {"data": {"attributes": {"token": "new-pat"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{conn.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        assert conn.token == "new-pat"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_422_installation_id_collision(self, *_mocks):
        """Changing github-installation-id to one another connection
        already uses must 422 (the #315 collision guard)."""
        conn = _mock_conn(provider="github", github_installation_id=111)
        other = _mock_conn(github_installation_id=222)
        app, db = _make_app(_admin())
        # First execute: load the target connection.
        # Second execute: duplicate-installation lookup → other found.
        db.execute = AsyncMock(side_effect=[_scalar_result(conn), _scalar_result(other)])
        body = {"data": {"attributes": {"github-installation-id": 222}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{conn.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "already connected" in resp.json()["detail"]
        assert conn.github_installation_id == 111

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_installation_id_change_no_collision(self, *_mocks):
        conn = _mock_conn(provider="github", github_installation_id=111)
        app, db = _make_app(_admin())
        db.execute = AsyncMock(side_effect=[_scalar_result(conn), _scalar_result(None)])
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        body = {"data": {"attributes": {"github-installation-id": 333}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{conn.id}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        assert conn.github_installation_id == 333

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_404_unknown_id(self, *_mocks):
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(None))
        body = {"data": {"attributes": {"name": "x"}}}
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{uuid.uuid4()}",
                json=body,
                headers=_AUTH,
            )
        assert resp.status_code == 404


# ── Delete ───────────────────────────────────────────────────────────────


class TestDeleteConnection:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_204_on_success(self, *_mocks):
        conn = _mock_conn()
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(conn))
        db.delete = AsyncMock()
        db.commit = AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"/api/terrapod/v1/vcs-connections/vcs-{conn.id}", headers=_AUTH)
        assert resp.status_code == 204
        db.delete.assert_awaited_once_with(conn)

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_404_when_missing(self, *_mocks):
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(None))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/terrapod/v1/vcs-connections/vcs-{uuid.uuid4()}", headers=_AUTH
            )
        assert resp.status_code == 404
