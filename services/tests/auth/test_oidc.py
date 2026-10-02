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
                {
                    "sub": "user-1",
                    "email": "user@example.com",
                    "email_verified": True,
                    "exp": 9999999999,
                }
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


class TestVouchedIdentity:
    """The email claim is the principal, so the IdP must vouch for it.

    These drive the real ``handle_callback`` rather than the guard helper: the
    whole point of GHSA-3m8x-ff8g-7x8c is that the callback path accepted whatever
    the claims contained, so a test that called the guard directly would keep
    passing if the callback stopped calling it.
    """

    def _connector(self, **overrides) -> OIDCConnector:
        cfg = {
            "name": "test-idp",
            "issuer_url": "https://idp.example.com",
            "client_id": "client-123",
            "client_secret": "secret",
        }
        cfg.update(overrides)
        return OIDCConnector(OIDCProviderConfig(**cfg))

    async def _callback(self, connector: OIDCConnector, claims: dict):
        """Drive handle_callback with `claims`, returning the identity it built."""
        with (
            patch.object(OIDCConnector, "_ensure_discovery", new_callable=AsyncMock) as disco,
            patch.object(OIDCConnector, "_ensure_jwks", new_callable=AsyncMock),
            patch("terrapod.auth.connectors.oidc.httpx.AsyncClient") as client_cls,
            patch("terrapod.auth.connectors.oidc.authlib_jwt.decode") as decode,
            patch.object(connector, "_fetch_userinfo", new_callable=AsyncMock) as userinfo,
        ):
            disco.return_value = {
                "issuer": "https://idp.example.com",
                "token_endpoint": "https://idp.example.com/oauth2/token",
            }
            response = MagicMock()
            response.raise_for_status = MagicMock()
            response.json.return_value = {"id_token": "h.p.s", "access_token": "opaque"}
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.__aexit__.return_value = False
            client.post.return_value = response
            client_cls.return_value = client
            decode.return_value = _FakeJwtClaims(claims)
            userinfo.return_value = {}

            return await connector.handle_callback(
                callback_url="https://terrapod.example.com/api/v1/auth/callback",
                code="auth-code",
                code_verifier="verifier",
            )

    async def test_a_verified_email_is_accepted(self):
        identity = await self._callback(
            self._connector(),
            {"sub": "user-1", "email": "user@example.com", "email_verified": True},
        )
        assert identity.email == "user@example.com"
        assert identity.subject == "user-1"

    async def test_email_verified_true_as_a_string_is_accepted(self):
        # Some IdPs send the claim as a JSON string rather than a boolean.
        identity = await self._callback(
            self._connector(),
            {"sub": "user-1", "email": "user@example.com", "email_verified": "true"},
        )
        assert identity.email == "user@example.com"

    async def test_an_explicitly_unverified_email_is_refused(self):
        with pytest.raises(ValueError, match="email_verified"):
            await self._callback(
                self._connector(),
                {"sub": "user-1", "email": "victim@example.com", "email_verified": False},
            )

    async def test_an_explicitly_unverified_email_is_refused_even_when_not_required(self):
        """`require_email_verified=false` relaxes ABSENT, never an explicit false.

        The relaxation exists for an IdP that verifies email but omits the claim.
        An IdP that actively reports the address as unverified is a different
        thing, and no configuration should make Terrapod trust it.
        """
        with pytest.raises(ValueError, match="email_verified"):
            await self._callback(
                self._connector(require_email_verified=False),
                {"sub": "user-1", "email": "victim@example.com", "email_verified": False},
            )

    async def test_the_string_false_is_refused(self):
        # Truthy to Python, so a bool() check would have accepted it.
        with pytest.raises(ValueError, match="email_verified"):
            await self._callback(
                self._connector(),
                {"sub": "user-1", "email": "victim@example.com", "email_verified": "false"},
            )

    async def test_an_absent_claim_is_refused_by_default(self):
        with pytest.raises(ValueError, match="email_verified"):
            await self._callback(
                self._connector(),
                {"sub": "user-1", "email": "user@example.com"},
            )

    async def test_an_absent_claim_is_accepted_when_the_provider_opts_out(self):
        identity = await self._callback(
            self._connector(require_email_verified=False),
            {"sub": "user-1", "email": "user@example.com"},
        )
        assert identity.email == "user@example.com"

    async def test_a_missing_email_is_refused(self):
        """Otherwise every such user collapses into one empty principal."""
        with pytest.raises(ValueError, match="email"):
            await self._callback(
                self._connector(require_email_verified=False),
                {"sub": "user-1", "email_verified": True},
            )

    async def test_a_missing_subject_is_refused(self):
        """`sub` is mandatory in OIDC and is the half that survives an email change."""
        with pytest.raises(ValueError, match="sub"):
            await self._callback(
                self._connector(),
                {"email": "user@example.com", "email_verified": True},
            )
