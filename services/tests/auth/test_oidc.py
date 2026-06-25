"""Tests for the upstream OIDC connector PKCE flow."""

import base64
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from terrapod.auth.connectors.oidc import OIDCConnector, _generate_pkce_pair
from terrapod.config import OIDCProviderConfig


class _FakeJwtClaims(dict):
    def validate(self, leeway: int = 30) -> None:
        pass


class TestGeneratePkcePair:
    def test_challenge_matches_verifier_s256(self):
        verifier, challenge = _generate_pkce_pair()
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert challenge == expected

    def test_verifier_is_unique(self):
        v1, _ = _generate_pkce_pair()
        v2, _ = _generate_pkce_pair()
        assert v1 != v2


class TestOIDCConnectorPkce:
    @pytest.fixture
    def connector(self) -> OIDCConnector:
        config = OIDCProviderConfig(
            name="test-idp",
            issuer_url="https://idp.example.com",
            client_id="client-123",
            client_secret="secret",
            scopes=["openid", "profile"],
        )
        return OIDCConnector(config)

    @patch.object(OIDCConnector, "_ensure_discovery", new_callable=AsyncMock)
    async def test_build_authorization_request_includes_pkce(
        self, mock_discovery: AsyncMock, connector: OIDCConnector
    ):
        mock_discovery.return_value = {
            "authorization_endpoint": "https://idp.example.com/oauth2/authorize",
        }

        req = await connector.build_authorization_request(
            callback_url="https://terrapod.example.com/api/terrapod/v1/auth/callback",
            state="idp-state-xyz",
        )

        assert req.code_verifier is not None
        parsed = urlparse(req.authorize_url)
        params = parse_qs(parsed.query)
        assert params["code_challenge_method"] == ["S256"]
        assert params["code_challenge"] == [
            base64.urlsafe_b64encode(hashlib.sha256(req.code_verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        ]

    @patch("terrapod.auth.connectors.oidc.httpx.AsyncClient")
    @patch.object(OIDCConnector, "_ensure_jwks", new_callable=AsyncMock)
    @patch.object(OIDCConnector, "_ensure_discovery", new_callable=AsyncMock)
    async def test_handle_callback_posts_code_verifier(
        self,
        mock_discovery: AsyncMock,
        mock_jwks: AsyncMock,
        mock_client_cls,
        connector: OIDCConnector,
    ):
        mock_discovery.return_value = {
            "issuer": "https://idp.example.com",
            "token_endpoint": "https://idp.example.com/oauth2/token",
            "userinfo_endpoint": "https://idp.example.com/userinfo",
        }

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "id_token": "header.payload.sig",
            "access_token": "opaque-access",
        }
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        with (
            patch("terrapod.auth.connectors.oidc.authlib_jwt.decode") as mock_decode,
            patch.object(connector, "_fetch_userinfo", new_callable=AsyncMock) as mock_userinfo,
        ):
            mock_decode.return_value = _FakeJwtClaims(
                {"sub": "user-1", "email": "user@example.com", "exp": 9999999999}
            )
            mock_userinfo.return_value = {}

            await connector.handle_callback(
                callback_url="https://terrapod.example.com/api/terrapod/v1/auth/callback",
                code="auth-code",
                code_verifier="upstream-verifier-abc",
            )

        posted = mock_client.post.call_args
        assert posted is not None
        token_data = posted.kwargs["data"]
        assert token_data["code_verifier"] == "upstream-verifier-abc"
