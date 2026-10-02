"""Tests for VCS connection CRUD endpoints (admin-only, JSON:API).

Covers the previously-untested router
`terrapod.api.routers.vcs_connections`, including the #315 PATCH
partial-update / credential-preservation surface.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
    webhook_secret=None,
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
    c.webhook_secret = webhook_secret
    # GHSA-v8g7-pqrj-8mcm added three columns the serializer reads. Set here
    # rather than per-test: a MagicMock left to invent them returns a Mock, which
    # is not JSON serialisable, so every response assertion in this file would fail
    # on a detail unrelated to what it tests. Values match the migration's server
    # defaults, which is what a pre-existing row actually holds.
    c.owner_email = ""
    c.labels = {}
    c.allowed_repositories = []
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


# ── Per-connection webhook secret (write-only) ────────────────────────────


class TestWebhookSecret:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_with_secret_sets_flag_and_never_echoes(self, *_mocks):
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(None))  # no dup install
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        body = {
            "data": {
                "attributes": {
                    "name": "gh-wh",
                    "provider": "github",
                    "github-app-id": 1,
                    "github-installation-id": 2,
                    "private-key": "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----",
                    "webhook-secret": "my-per-conn-secret",
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/vcs-connections", json=body, headers=_AUTH)
        assert resp.status_code == 201, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["has-webhook-secret"] is True
        # The raw value must never be echoed anywhere in the response.
        assert "webhook-secret" not in attrs
        assert "my-per-conn-secret" not in resp.text

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_without_secret_flag_false(self, *_mocks):
        app, db = _make_app(_admin())
        db.execute = AsyncMock(return_value=_scalar_result(None))
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        body = {
            "data": {
                "attributes": {
                    "name": "gh-nowh",
                    "provider": "github",
                    "github-app-id": 1,
                    "github-installation-id": 3,
                    "private-key": "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----",
                }
            }
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post("/api/terrapod/v1/vcs-connections", json=body, headers=_AUTH)
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["attributes"]["has-webhook-secret"] is False

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.vcs_connections._get_connection", new_callable=AsyncMock)
    async def test_patch_rotate_set_clear_omit(self, mock_get, *_mocks):
        app, db = _make_app(_admin())
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        # Rotate: non-empty value → set.
        conn = _mock_conn(webhook_secret=None)
        mock_get.return_value = conn
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{conn.id}",
                json={"data": {"attributes": {"webhook-secret": "rotated"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        assert conn.webhook_secret == "rotated"

        # Clear: explicit empty string → None (fall back to global).
        conn2 = _mock_conn(webhook_secret="existing")
        mock_get.return_value = conn2
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{conn2.id}",
                json={"data": {"attributes": {"webhook-secret": ""}}},
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        assert conn2.webhook_secret is None

        # Omit: key absent → untouched.
        conn3 = _mock_conn(webhook_secret="keep")
        mock_get.return_value = conn3
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/vcs-connections/vcs-{conn3.id}",
                json={"data": {"attributes": {"name": "renamed"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 200, resp.text
        assert conn3.webhook_secret == "keep"


class TestRateLimitAttributes:
    """The budget is surfaced on the connection (#1334).

    The distinction the tests exist to protect: null means "the server does not
    report a rate limit", 0 means "there is none left". Collapsing them would
    make the indicator either permanently alarming or quietly useless.
    """

    def _conn(self):
        from datetime import UTC, datetime

        c = MagicMock()
        c.id = uuid.uuid4()
        c.name = "gh"
        c.provider = "github"
        c.server_url = ""
        c.status = "active"
        c.token = "x"
        c.webhook_secret = None
        c.github_app_id = 1
        c.github_installation_id = 2
        c.github_account_login = "org"
        c.github_account_type = "Organization"
        c.owner_email = ""
        c.labels = {}
        c.allowed_repositories = []
        c.created_at = datetime.now(UTC)
        c.updated_at = datetime.now(UTC)
        return c

    def test_a_connection_with_no_observation_reports_null_not_zero(self):
        from terrapod.api.routers.vcs_connections import _connection_json

        attrs = _connection_json(self._conn(), quota=None, consumption=None)["attributes"]
        assert attrs["rate-limit"] is None
        assert attrs["rate-limit-remaining"] is None
        assert attrs["rate-limit-observed-at"] is None

    def test_an_exhausted_budget_reports_zero_not_null(self):
        import time

        from terrapod.api.routers.vcs_connections import _connection_json
        from terrapod.services.vcs_rate_limit import RateLimitSnapshot

        now = int(time.time())
        snap = RateLimitSnapshot(
            limit=5000,
            remaining=0,
            reset_at=now + 900,
            observed_at=now,
            resource="core",
            window_seconds=3600,
        )
        attrs = _connection_json(self._conn(), quota=snap, consumption=None)["attributes"]
        assert attrs["rate-limit"] == 5000
        assert attrs["rate-limit-remaining"] == 0, "exhausted must not read as 'not reported'"
        assert attrs["rate-limit-observed-at"].endswith("Z"), "RFC3339 with Z (rule 10)"
        assert attrs["rate-limit-reset-at"].endswith("Z")

    def test_the_keys_are_always_present(self):
        """A consumer should never have to distinguish 'absent key' from 'null'."""
        from terrapod.api.routers.vcs_connections import _connection_json

        attrs = _connection_json(self._conn(), quota=None, consumption=None)["attributes"]
        for k in (
            "rate-limit",
            "rate-limit-remaining",
            "rate-limit-resource",
            "rate-limit-reset-at",
            "rate-limit-observed-at",
        ):
            assert k in attrs


class TestTheAllowlistCannotBeWidenedByAccident:
    """An empty `allowed-repositories` means ANY repository the credential can
    reach, which makes "drop the blank entries" a dangerous convenience: a
    fat-fingered pattern collapsed the list to empty and answered 200, leaving the
    connection WIDER than before with nothing said. Found in review of the fix
    itself.
    """

    def test_a_list_of_only_blanks_is_refused(self):
        from fastapi import HTTPException

        from terrapod.api.routers.vcs_connections import _rbac_attrs

        for payload in (
            {"allowed-repositories": ["   "]},
            {"allowed-repositories": ["", "  ", "\t"]},
        ):
            with pytest.raises(HTTPException) as exc:
                _rbac_attrs(payload)
            assert exc.value.status_code == 422
            assert "blank" in str(exc.value.detail)

    def test_a_deliberately_empty_list_still_means_any(self):
        """The refusal must not take away the only way to widen scope again."""
        from terrapod.api.routers.vcs_connections import _rbac_attrs

        assert _rbac_attrs({"allowed-repositories": []})[2] == []

    def test_blanks_mixed_with_a_real_pattern_are_dropped_not_refused(self):
        """Still a narrowing, so there is nothing to warn about."""
        from terrapod.api.routers.vcs_connections import _rbac_attrs

        assert _rbac_attrs({"allowed-repositories": ["myorg/*", "  "]})[2] == ["myorg/*"]

    def test_the_docstring_lists_the_fields_the_handler_actually_edits(self):
        """The docstring named only name/server-url/status/App-ids while the code
        twenty lines below edited three more — the exact drift this project's own
        notes call out, and the first thing a reader checking "can I PATCH the
        allowlist?" would land on."""
        import inspect

        from terrapod.api.routers.vcs_connections import update_connection

        doc = inspect.getdoc(update_connection) or ""
        for attr in ("owner-email", "labels", "allowed-repositories"):
            assert attr in doc, f"{attr} is editable here but the docstring omits it"


class TestTheServerDoesNotTransformWhatTheProviderSends:
    """`terrapod_vcs_connection` manages these three attributes, and a provider
    writes the server's response back into state. So a server-side transform makes
    the plan disagree with the result and the apply fails with "Provider produced
    inconsistent result after apply" — the resource becomes unmanageable as code,
    which for a security control means it stops being adjusted.

    Both transforms these tests pin were added for a reason that turned out to be
    already handled on the read side: `may_reference_connection` folds case on both
    sides of the owner comparison, and `repository_allowed` strips every pattern
    before matching. So the fix costs nothing at all.
    """

    def test_a_mixed_case_owner_is_stored_exactly_as_sent(self):
        from terrapod.api.routers.vcs_connections import _rbac_attrs

        owner, _, _ = _rbac_attrs({"owner-email": "Owner@Example.COM"})
        assert owner == "Owner@Example.COM"

    def test_surrounding_whitespace_on_the_owner_survives(self):
        from terrapod.api.routers.vcs_connections import _rbac_attrs

        owner, _, _ = _rbac_attrs({"owner-email": " owner@example.com "})
        assert owner == " owner@example.com "

    def test_a_mixed_case_owner_still_matches_at_read_time(self):
        """The reason the fold is safe to remove, asserted rather than assumed."""
        import asyncio
        from unittest.mock import AsyncMock

        from terrapod.db.models import VCSConnection
        from terrapod.services.vcs_connection_rbac import may_reference_connection

        conn = VCSConnection(id=uuid.uuid4(), owner_email="Owner@Example.COM")
        db = AsyncMock()
        db.get = AsyncMock(return_value=conn)
        allowed = asyncio.run(
            may_reference_connection(
                db,
                conn_id=conn.id,
                actor_email="owner@example.com",
                is_platform_admin=False,
            )
        )
        assert allowed is True

    def test_a_pattern_keeps_its_whitespace_and_still_matches(self):
        from terrapod.api.routers.vcs_connections import _rbac_attrs
        from terrapod.db.models import VCSConnection
        from terrapod.services.vcs_connection_rbac import repository_allowed

        _, _, repos = _rbac_attrs({"allowed-repositories": [" myorg/* "]})
        assert repos == [" myorg/* "]

        conn = VCSConnection(provider="github", allowed_repositories=repos)
        assert repository_allowed(conn, "https://github.com/myorg/thing") is True

    def test_a_non_string_owner_is_refused_not_a_500(self):
        from fastapi import HTTPException

        from terrapod.api.routers.vcs_connections import _rbac_attrs

        with pytest.raises(HTTPException) as exc:
            _rbac_attrs({"owner-email": 123})
        assert exc.value.status_code == 422
        assert "owner-email" in str(exc.value.detail)

    def test_an_over_length_owner_is_refused_not_truncated(self):
        from fastapi import HTTPException

        from terrapod.api.routers.vcs_connections import _rbac_attrs

        with pytest.raises(HTTPException) as exc:
            _rbac_attrs({"owner-email": "a" * 300})
        assert exc.value.status_code == 422


class TestAStoredRowCannotBeMadeUneditable:
    """The partial-update path builds a merged dict carrying STORED values for the
    keys the caller omitted. Validating those is the #316 trap: a row written by a
    migration or by hand is refused on every subsequent edit, so the only way to fix
    it is the one way that is blocked. Validation applies to what was sent.
    """

    def test_patching_labels_is_not_refused_by_a_stored_blank_pattern(self):
        from terrapod.api.routers.vcs_connections import _rbac_attrs

        # What a PATCH of `labels` alone builds when the row holds `["  "]`.
        merged = {
            "owner-email": "",
            "labels": {"team": "net"},
            "allowed-repositories": ["  "],
        }
        _, labels, _ = _rbac_attrs(merged, supplied={"labels"})
        assert labels == {"team": "net"}

    def test_patching_labels_is_not_refused_by_a_stored_reserved_label(self):
        from terrapod.api.routers.vcs_connections import _rbac_attrs

        merged = {
            "owner-email": "",
            "labels": {"status": "stuck"},
            "allowed-repositories": ["myorg/*"],
        }
        _, _, repos = _rbac_attrs(merged, supplied={"allowed-repositories"})
        assert repos == ["myorg/*"]

    def test_but_a_supplied_blank_allowlist_is_still_refused(self):
        from fastapi import HTTPException

        from terrapod.api.routers.vcs_connections import _rbac_attrs

        with pytest.raises(HTTPException) as exc:
            _rbac_attrs({"allowed-repositories": ["  "]}, supplied={"allowed-repositories"})
        assert exc.value.status_code == 422

    def test_and_a_supplied_reserved_label_is_still_refused(self):
        from fastapi import HTTPException

        from terrapod.api.routers.vcs_connections import _rbac_attrs

        with pytest.raises(HTTPException) as exc:
            _rbac_attrs({"labels": {"status": "nope"}}, supplied={"labels"})
        assert exc.value.status_code == 422
