"""Tests for TFE V2 compatibility endpoints — ping, account/details, token CRUD."""

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_app
from terrapod.api.dependencies import AuthenticatedUser
from terrapod.api.routers.tfe_v2 import TFP_API_VERSION, TFP_APP_NAME, X_TFE_VERSION


def _make_app_with_auth(user: AuthenticatedUser | None = None):
    """Create an app with auth dependency overridden."""
    app = create_app()

    if user is not None:
        from terrapod.api.dependencies import get_current_user

        async def override_auth():
            return user

        app.dependency_overrides[get_current_user] = override_auth

    from terrapod.db.session import get_db

    async def override_db():
        return AsyncMock()

    app.dependency_overrides[get_db] = override_db

    return app


class TestPing:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_ping_returns_correct_headers(
        self, mock_init_db, mock_init_redis, mock_init_storage
    ):
        app = create_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v2/ping")

        assert response.status_code == 200
        assert response.headers["TFP-API-Version"] == TFP_API_VERSION
        assert response.headers["TFP-AppName"] == TFP_APP_NAME
        assert response.headers["X-TFE-Version"] == X_TFE_VERSION

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_ping_no_auth_required(self, mock_init_db, mock_init_redis, mock_init_storage):
        """Ping endpoint works without any Authorization header."""
        app = create_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v2/ping")

        assert response.status_code == 200


class TestAccountDetails:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_account_details_returns_jsonapi_format(
        self, mock_init_db, mock_init_redis, mock_init_storage
    ):
        user = AuthenticatedUser(
            email="test@example.com",
            display_name="Test User",
            roles=["admin"],
            provider_name="local",
            auth_method="session",
        )
        app = _make_app_with_auth(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v2/account/details",
                headers={"Authorization": "Bearer dummy-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["type"] == "users"
        assert data["id"] == "test"
        assert data["attributes"]["username"] == "test"
        assert data["attributes"]["email"] == "test@example.com"
        assert data["attributes"]["is-service-account"] is False
        assert data["attributes"]["permissions"]["can-create-organizations"] is True

        # Should also have TFE headers
        assert response.headers["TFP-API-Version"] == TFP_API_VERSION

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_account_details_api_token_is_service_account(
        self, mock_init_db, mock_init_redis, mock_init_storage
    ):
        user = AuthenticatedUser(
            email="bot@example.com",
            display_name=None,
            roles=[],
            provider_name="api_token",
            auth_method="api_token",
        )
        app = _make_app_with_auth(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v2/account/details",
                headers={"Authorization": "Bearer dummy-token"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["attributes"]["is-service-account"] is True
        assert data["attributes"]["permissions"]["can-create-organizations"] is False

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_account_details_requires_auth(
        self, mock_init_db, mock_init_redis, mock_init_storage
    ):
        app = create_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/v2/account/details")

        assert response.status_code in (401, 403)


class TestTokenCRUD:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tokens.create_api_token")
    async def test_create_token(
        self,
        mock_create,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        from datetime import datetime

        mock_token = MagicMock()
        mock_token.id = "at-abc123"
        mock_token.description = "my token"
        mock_token.token_type = "user"
        mock_token.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        mock_token.last_used_at = None
        mock_create.return_value = (mock_token, "raw.tpod.secret")

        user = AuthenticatedUser(
            email="test@example.com",
            display_name="Test",
            roles=[],
            provider_name="local",
            auth_method="session",
        )
        app = _make_app_with_auth(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v2/users/test/authentication-tokens",
                json={
                    "data": {
                        "type": "authentication-tokens",
                        "attributes": {"description": "my token"},
                    }
                },
                headers={"Authorization": "Bearer dummy"},
            )

        assert response.status_code == 201
        data = response.json()["data"]
        assert data["type"] == "authentication-tokens"
        assert data["id"] == "at-abc123"
        assert data["attributes"]["token"] == "raw.tpod.secret"
        assert data["attributes"]["description"] == "my token"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tokens.list_user_tokens")
    async def test_list_tokens(
        self,
        mock_list,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        from datetime import datetime

        mock_token = MagicMock()
        mock_token.id = "at-abc123"
        mock_token.description = "test"
        mock_token.token_type = "user"
        mock_token.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        mock_token.last_used_at = None
        mock_list.return_value = [mock_token]

        user = AuthenticatedUser(
            email="test@example.com",
            display_name="Test",
            roles=[],
            provider_name="local",
            auth_method="session",
        )
        app = _make_app_with_auth(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v2/users/test/authentication-tokens",
                headers={"Authorization": "Bearer dummy"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["id"] == "at-abc123"
        # Token value should be null (not creation time)
        assert data[0]["attributes"]["token"] is None

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tokens.get_token_by_id")
    @patch("terrapod.api.routers.tokens.revoke_token")
    async def test_delete_token(
        self,
        mock_revoke,
        mock_get_token,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        mock_token = MagicMock()
        mock_token.id = "at-abc123"
        mock_token.user_email = "test@example.com"
        mock_get_token.return_value = mock_token
        mock_revoke.return_value = True

        user = AuthenticatedUser(
            email="test@example.com",
            display_name="Test",
            roles=[],
            provider_name="local",
            auth_method="session",
        )
        app = _make_app_with_auth(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(
                "/api/v2/authentication-tokens/at-abc123",
                headers={"Authorization": "Bearer dummy"},
            )

        assert response.status_code == 204

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tokens.get_token_by_id")
    async def test_delete_other_users_token_forbidden(
        self,
        mock_get_token,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        mock_token = MagicMock()
        mock_token.id = "at-abc123"
        mock_token.user_email = "other@example.com"
        mock_get_token.return_value = mock_token

        user = AuthenticatedUser(
            email="test@example.com",
            display_name="Test",
            roles=[],
            provider_name="local",
            auth_method="session",
        )
        app = _make_app_with_auth(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.delete(
                "/api/v2/authentication-tokens/at-abc123",
                headers={"Authorization": "Bearer dummy"},
            )

        assert response.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_token_for_other_user_forbidden(
        self, mock_init_db, mock_init_redis, mock_init_storage
    ):
        user = AuthenticatedUser(
            email="test@example.com",
            display_name="Test",
            roles=[],
            provider_name="local",
            auth_method="session",
        )
        app = _make_app_with_auth(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v2/users/other-user/authentication-tokens",
                json={"data": {"type": "authentication-tokens", "attributes": {}}},
                headers={"Authorization": "Bearer dummy"},
            )

        assert response.status_code == 403
