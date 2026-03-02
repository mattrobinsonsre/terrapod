"""Tests for OAuth2 terraform login flow.

Tests the service discovery, /oauth/authorize, /oauth/token endpoints
and PKCE verification.
"""

import base64
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.routers.oauth import _verify_pkce
from terrapod.db.session import get_db


class TestVerifyPKCE:
    def test_valid_s256_challenge(self):
        code_verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        assert _verify_pkce(code_verifier, code_challenge, "S256") is True

    def test_invalid_verifier(self):
        code_verifier = "correct-verifier"
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        assert _verify_pkce("wrong-verifier", code_challenge, "S256") is False

    def test_unsupported_method(self):
        assert _verify_pkce("verifier", "challenge", "plain") is False


class TestTerraformServiceDiscovery:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_well_known_terraform_json(
        self, mock_init_db, mock_init_redis, mock_init_storage
    ):
        app = create_app()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/.well-known/terraform.json")

        assert response.status_code == 200
        data = response.json()
        assert "login.v1" in data
        assert data["login.v1"]["grant_types"] == ["authz_code"]
        assert data["login.v1"]["authz"] == "/oauth/authorize"
        assert data["login.v1"]["token"] == "/oauth/token"
        assert data["login.v1"]["ports"] == [10000, 10010]
        assert data["tfe.v2"] == "/api/v2/"
        assert data["tfe.v2.1"] == "/api/v2/"
        assert data["tfe.v2.2"] == "/api/v2/"


class TestOAuthAuthorize:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.oauth.store_auth_state")
    @patch("terrapod.api.routers.oauth.get_default_connector")
    async def test_authorize_redirects_to_provider(
        self,
        mock_get_connector,
        mock_store_state,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        mock_connector = AsyncMock()
        mock_connector.name = "test-oidc"
        mock_connector.build_authorization_request.return_value = MagicMock(
            authorize_url="https://idp.example.com/authorize?state=xyz",
            state="xyz",
            nonce="nonce-123",
        )
        mock_get_connector.return_value = mock_connector

        app = create_app()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            response = await client.get(
                "/oauth/authorize",
                params={
                    "client_id": "terraform-cli",
                    "redirect_uri": "http://localhost:10000/login",
                    "state": "client-state",
                    "code_challenge": "challenge123",
                    "code_challenge_method": "S256",
                },
            )

        assert response.status_code == 302
        assert "idp.example.com" in response.headers["location"]
        mock_store_state.assert_called_once()

        # Verify stored state has credential_type="api_token"
        stored_state = mock_store_state.call_args[0][0]
        assert stored_state.credential_type == "api_token"
        assert stored_state.code_challenge == "challenge123"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.oauth.get_default_connector")
    async def test_authorize_rejects_non_code_response_type(
        self,
        mock_get_connector,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/oauth/authorize",
                params={
                    "response_type": "token",
                    "client_id": "terraform-cli",
                    "redirect_uri": "http://localhost:10000/login",
                    "code_challenge": "challenge123",
                    "code_challenge_method": "S256",
                },
            )

        assert response.status_code == 400

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.oauth.get_default_connector")
    async def test_authorize_rejects_no_provider(
        self,
        mock_get_connector,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        mock_get_connector.return_value = None

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/oauth/authorize",
                params={
                    "client_id": "terraform-cli",
                    "redirect_uri": "http://localhost:10000/login",
                    "code_challenge": "challenge123",
                    "code_challenge_method": "S256",
                },
            )

        assert response.status_code == 400
        assert "No auth provider" in response.json()["detail"]


class TestOAuthToken:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.oauth.create_api_token")
    @patch("terrapod.api.routers.oauth.consume_auth_code")
    async def test_token_exchange_creates_api_token(
        self,
        mock_consume_code,
        mock_create_token,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        # Set up PKCE
        code_verifier = "test-code-verifier-that-is-long-enough"
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        mock_consume_code.return_value = MagicMock(
            email="test@example.com",
            roles=["admin"],
            provider_name="oidc",
            code_challenge=code_challenge,
            code_challenge_method="S256",
        )

        mock_token = MagicMock(id="at-test123")
        mock_create_token.return_value = (mock_token, "raw-token.tpod.secret")

        # Mock db dependency
        app = create_app()

        mock_db = AsyncMock()

        async def override_get_db():
            return mock_db

        app.dependency_overrides[get_db] = override_get_db

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "test-code",
                    "code_verifier": code_verifier,
                    "client_id": "terraform-cli",
                    "redirect_uri": "http://localhost:10000/login",
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["access_token"] == "raw-token.tpod.secret"
        assert data["token_type"] == "bearer"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.oauth.consume_auth_code", new_callable=AsyncMock)
    async def test_token_exchange_rejects_invalid_code(
        self,
        mock_consume_code,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        mock_consume_code.return_value = None

        app = create_app()

        async def override_get_db():
            return AsyncMock()

        app.dependency_overrides[get_db] = override_get_db

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "invalid-code",
                    "code_verifier": "verifier",
                },
            )

        assert response.status_code == 401

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.oauth.consume_auth_code", new_callable=AsyncMock)
    async def test_token_exchange_rejects_bad_pkce(
        self,
        mock_consume_code,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        mock_consume_code.return_value = MagicMock(
            email="test@example.com",
            roles=[],
            provider_name="oidc",
            code_challenge="stored-challenge",
            code_challenge_method="S256",
        )

        app = create_app()

        async def override_get_db():
            return AsyncMock()

        app.dependency_overrides[get_db] = override_get_db

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": "test-code",
                    "code_verifier": "wrong-verifier",
                },
            )

        assert response.status_code == 401
        assert "PKCE" in response.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_token_exchange_rejects_wrong_grant_type(
        self, mock_init_db, mock_init_redis, mock_init_storage
    ):
        app = create_app()

        async def override_get_db():
            return AsyncMock()

        app.dependency_overrides[get_db] = override_get_db

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "code": "test-code",
                    "code_verifier": "verifier",
                },
            )

        assert response.status_code == 400
