"""Tests for SSO connector registry and base abstractions."""

from unittest.mock import patch

from terrapod.auth.connectors import (
    _connectors,
    get_connector,
    get_default_connector,
    init_connectors,
    list_connectors,
)
from terrapod.auth.sso import AuthenticatedIdentity, AuthorizationRequest, SSOConnector


class TestSSOConnectorABC:
    def test_authenticated_identity_defaults(self):
        identity = AuthenticatedIdentity(
            provider_name="test",
            subject="user-123",
            email="test@example.com",
        )
        assert identity.display_name is None
        assert identity.groups == []
        assert identity.raw_claims == {}
        assert identity.id_token_expires_at is None

    def test_authenticated_identity_with_all_fields(self):
        identity = AuthenticatedIdentity(
            provider_name="okta",
            subject="user-456",
            email="test@example.com",
            display_name="Test User",
            groups=["admin", "dev"],
            raw_claims={"sub": "user-456"},
        )
        assert identity.groups == ["admin", "dev"]
        assert identity.raw_claims == {"sub": "user-456"}

    def test_authorization_request_defaults(self):
        req = AuthorizationRequest(
            authorize_url="https://idp.example.com/authorize",
            state="state-123",
        )
        assert req.nonce is None

    def test_connector_display_name_defaults_to_name(self):
        """SSOConnector.display_name falls back to name."""

        class TestConnector(SSOConnector):
            @property
            def name(self) -> str:
                return "my-test-provider"

            @property
            def provider_type(self) -> str:
                return "oidc"

            async def build_authorization_request(self, callback_url, state):
                pass

            async def handle_callback(self, callback_url, **kwargs):
                pass

        connector = TestConnector()
        assert connector.display_name == "my-test-provider"


class TestConnectorRegistry:
    @patch("terrapod.auth.connectors.settings")
    def test_init_connectors_local_only(self, mock_settings):
        mock_settings.auth.local_enabled = True
        mock_settings.auth.sso.oidc = []
        mock_settings.auth.sso.saml = []

        init_connectors()

        assert "local" in _connectors
        assert len(_connectors) == 1

    @patch("terrapod.auth.connectors.settings")
    def test_init_connectors_clears_previous(self, mock_settings):
        mock_settings.auth.local_enabled = True
        mock_settings.auth.sso.oidc = []
        mock_settings.auth.sso.saml = []

        init_connectors()
        assert len(_connectors) == 1

        mock_settings.auth.local_enabled = False
        init_connectors()
        assert len(_connectors) == 0

    @patch("terrapod.auth.connectors.settings")
    def test_get_connector_returns_registered(self, mock_settings):
        mock_settings.auth.local_enabled = True
        mock_settings.auth.sso.oidc = []
        mock_settings.auth.sso.saml = []

        init_connectors()

        connector = get_connector("local")
        assert connector is not None
        assert connector.name == "local"

    @patch("terrapod.auth.connectors.settings")
    def test_get_connector_returns_none_for_unknown(self, mock_settings):
        mock_settings.auth.local_enabled = True
        mock_settings.auth.sso.oidc = []
        mock_settings.auth.sso.saml = []

        init_connectors()

        assert get_connector("nonexistent") is None

    @patch("terrapod.auth.connectors.settings")
    def test_list_connectors(self, mock_settings):
        mock_settings.auth.local_enabled = True
        mock_settings.auth.sso.oidc = []
        mock_settings.auth.sso.saml = []

        init_connectors()

        providers = list_connectors()
        assert len(providers) == 1
        assert providers[0]["name"] == "local"
        assert providers[0]["type"] == "local"

    @patch("terrapod.auth.connectors.settings")
    def test_get_default_connector_with_explicit_default(self, mock_settings):
        mock_settings.auth.local_enabled = True
        mock_settings.auth.sso.oidc = []
        mock_settings.auth.sso.saml = []
        mock_settings.auth.sso.default_provider = "local"

        init_connectors()

        connector = get_default_connector()
        assert connector is not None
        assert connector.name == "local"

    @patch("terrapod.auth.connectors.settings")
    def test_get_default_connector_falls_back_to_first(self, mock_settings):
        mock_settings.auth.local_enabled = True
        mock_settings.auth.sso.oidc = []
        mock_settings.auth.sso.saml = []
        mock_settings.auth.sso.default_provider = ""

        init_connectors()

        connector = get_default_connector()
        assert connector is not None

    @patch("terrapod.auth.connectors.settings")
    def test_get_default_connector_returns_none_when_empty(self, mock_settings):
        mock_settings.auth.local_enabled = False
        mock_settings.auth.sso.oidc = []
        mock_settings.auth.sso.saml = []
        mock_settings.auth.sso.default_provider = ""

        init_connectors()

        assert get_default_connector() is None
