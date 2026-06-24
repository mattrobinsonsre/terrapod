"""Tests for the GPG-key management router (provider signing keys).

Covers create (happy + invalid-armor 422), list, show (happy + bad-uuid
404 + not-found 404), delete (happy 204 + not-found 404), and the
unauthenticated 401 gate.
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
_KEYS = "/api/terrapod/v1/gpg-keys"


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


def _mock_key(key_id="1C11AB2FF1189D6C"):
    k = MagicMock()
    k.id = uuid.uuid4()
    k.key_id = key_id
    k.ascii_armor = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...\n-----END..."
    k.source = "terrapod"
    k.source_url = None
    k.created_at = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
    k.updated_at = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
    return k


def _create_body():
    return {
        "data": {
            "type": "gpg-keys",
            "attributes": {
                "namespace": "default",
                "ascii-armor": "-----BEGIN PGP PUBLIC KEY BLOCK-----\n...\n-----END...",
            },
        }
    }


class TestAuth:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_list_no_auth_401(self, *mocks):
        app = create_app()
        app.dependency_overrides[get_db] = lambda: AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(_KEYS)
        assert resp.status_code == 401


class TestCreate:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.create_gpg_key", new_callable=AsyncMock)
    async def test_happy_path_201(self, mock_create, *mocks):
        mock_create.return_value = _mock_key()
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(_KEYS, json=_create_body(), headers=_AUTH)
        assert resp.status_code == 201
        body = resp.json()
        assert body["data"]["type"] == "gpg-keys"
        assert body["data"]["attributes"]["key-id"] == "1C11AB2FF1189D6C"
        assert body["data"]["attributes"]["namespace"] == "default"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.create_gpg_key", new_callable=AsyncMock)
    async def test_invalid_armor_422(self, mock_create, *mocks):
        mock_create.side_effect = ValueError("no PGP block found")
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(_KEYS, json=_create_body(), headers=_AUTH)
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_missing_armor_422(self, *mocks):
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                _KEYS,
                json={"data": {"type": "gpg-keys", "attributes": {"namespace": "default"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422


class TestList:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.list_gpg_keys", new_callable=AsyncMock)
    async def test_list_returns_keys(self, mock_list, *mocks):
        mock_list.return_value = [_mock_key(), _mock_key(key_id="AAAA1111BBBB2222")]
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(_KEYS, headers=_AUTH)
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 2


class TestShow:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.get_gpg_key", new_callable=AsyncMock)
    async def test_show_happy(self, mock_get, *mocks):
        key = _mock_key()
        mock_get.return_value = key
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"{_KEYS}/{key.id}", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["key-id"] == "1C11AB2FF1189D6C"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_show_bad_uuid_404(self, *mocks):
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"{_KEYS}/not-a-uuid", headers=_AUTH)
        assert resp.status_code == 404

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.get_gpg_key", new_callable=AsyncMock)
    async def test_show_not_found_404(self, mock_get, *mocks):
        mock_get.return_value = None
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"{_KEYS}/{uuid.uuid4()}", headers=_AUTH)
        assert resp.status_code == 404


class TestDelete:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.delete_gpg_key", new_callable=AsyncMock)
    async def test_delete_happy_204(self, mock_delete, *mocks):
        mock_delete.return_value = True
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"{_KEYS}/{uuid.uuid4()}", headers=_AUTH)
        assert resp.status_code == 204

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.gpg_keys.delete_gpg_key", new_callable=AsyncMock)
    async def test_delete_not_found_404(self, mock_delete, *mocks):
        mock_delete.return_value = False
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"{_KEYS}/{uuid.uuid4()}", headers=_AUTH)
        assert resp.status_code == 404

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_delete_bad_uuid_404(self, *mocks):
        app = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"{_KEYS}/xyz", headers=_AUTH)
        assert resp.status_code == 404
