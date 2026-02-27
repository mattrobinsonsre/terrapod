"""SAML identity provider connector.

Uses python3-saml for metadata parsing and assertion validation.
"""

from typing import Any

from terrapod.auth.sso import AuthenticatedIdentity, AuthorizationRequest, SSOConnector
from terrapod.config import SAMLProviderConfig
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


class SAMLConnector(SSOConnector):
    """SAML 2.0 identity provider connector."""

    def __init__(self, config: SAMLProviderConfig) -> None:
        self._config = config
        self._idp_metadata: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def display_name(self) -> str:
        return self._config.display_name or self._config.name

    @property
    def provider_type(self) -> str:
        return "saml"

    def _get_saml_settings(self, acs_url: str) -> dict[str, Any]:
        """Build python3-saml settings dict."""
        return {
            "strict": True,
            "sp": {
                "entityId": self._config.entity_id,
                "assertionConsumerService": {
                    "url": acs_url,
                    "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
                },
            },
            "idp": {},  # Populated from metadata
        }

    async def build_authorization_request(
        self,
        callback_url: str,
        state: str,
    ) -> AuthorizationRequest:
        """Build the SAML authorization redirect URL.

        For SAML, this creates an AuthnRequest and returns the IDP's SSO URL
        with the SAMLRequest parameter.
        """
        try:
            from onelogin.saml2.auth import OneLogin_Saml2_Auth
            from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
        except ImportError as e:
            raise RuntimeError(
                "python3-saml is required for SAML providers. "
                "Install it with: poetry add python3-saml"
            ) from e

        # Parse IDP metadata
        if self._idp_metadata is None:
            self._idp_metadata = OneLogin_Saml2_IdPMetadataParser.parse_remote(
                self._config.metadata_url
            )

        saml_settings = self._get_saml_settings(callback_url)
        saml_settings.update(self._idp_metadata)

        # Build a mock request for python3-saml
        request_data = {
            "https": "on",
            "http_host": "",
            "script_name": "",
            "get_data": {},
            "post_data": {},
        }

        auth = OneLogin_Saml2_Auth(request_data, saml_settings)
        sso_url = auth.login(return_to=state)

        # The SSO URL contains the SAMLRequest + RelayState
        return AuthorizationRequest(
            authorize_url=sso_url,
            state=state,
        )

    async def handle_callback(
        self,
        callback_url: str,
        **kwargs: Any,
    ) -> AuthenticatedIdentity:
        """Handle the SAML assertion callback."""
        saml_response = kwargs["saml_response"]
        relay_state = kwargs.get("relay_state", "")

        try:
            from onelogin.saml2.auth import OneLogin_Saml2_Auth
            from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
        except ImportError as e:
            raise RuntimeError("python3-saml is required for SAML providers.") from e

        if self._idp_metadata is None:
            self._idp_metadata = OneLogin_Saml2_IdPMetadataParser.parse_remote(
                self._config.metadata_url
            )

        saml_settings = self._get_saml_settings(callback_url)
        saml_settings.update(self._idp_metadata)

        request_data = {
            "https": "on",
            "http_host": "",
            "script_name": "",
            "get_data": {},
            "post_data": {"SAMLResponse": saml_response, "RelayState": relay_state},
        }

        auth = OneLogin_Saml2_Auth(request_data, saml_settings)
        auth.process_response()

        errors = auth.get_errors()
        if errors:
            raise ValueError(f"SAML validation failed for {self.name}: {errors}")

        if not auth.is_authenticated():
            raise ValueError(f"SAML authentication failed for {self.name}")

        attributes = auth.get_attributes()
        name_id = auth.get_nameid()

        # Extract identity from SAML attributes
        email = (
            attributes.get("email", [None])[0]
            or attributes.get(
                "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress", [None]
            )[0]
            or name_id
            or ""
        )
        display_name = (
            attributes.get("displayName", [None])[0]
            or attributes.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name", [None])[
                0
            ]
        )
        groups = attributes.get("groups", []) or attributes.get(
            "http://schemas.xmlsoap.org/claims/Group", []
        )

        # Build raw claims from all attributes
        raw_claims: dict[str, Any] = {"nameId": name_id}
        raw_claims.update(dict(attributes.items()))

        logger.info(
            "SAML authentication successful",
            provider=self.name,
            subject=name_id,
            email=email,
        )

        return AuthenticatedIdentity(
            provider_name=self.name,
            subject=name_id or "",
            email=email,
            display_name=display_name,
            groups=groups,
            raw_claims=raw_claims,
        )
