"""SSO connector base abstraction.

Defines the interface all identity provider connectors must implement,
plus the AuthenticatedIdentity returned after a successful SSO callback.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class AuthorizationRequest:
    """Data needed to redirect the user to the IDP."""

    authorize_url: str
    state: str  # IDP-facing state (not the client-facing state)
    nonce: str | None = None  # OIDC nonce for replay protection


@dataclass
class AuthenticatedIdentity:
    """Identity returned after successful IDP authentication."""

    provider_name: str
    subject: str  # IDP's unique identifier for this user
    email: str
    display_name: str | None = None
    groups: list[str] = field(default_factory=list)
    raw_claims: dict[str, Any] = field(default_factory=dict)
    # Set by OIDC connector when the id_token has an 'exp' claim. Used to
    # cap the session TTL so it never outlives the IDP token.
    id_token_expires_at: datetime | None = None


class SSOConnector(ABC):
    """Abstract base class for all identity provider connectors."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider name (e.g., 'auth0', 'okta')."""

    @property
    def display_name(self) -> str:
        """Human-readable label for login UI. Falls back to name."""
        return self.name

    @property
    @abstractmethod
    def provider_type(self) -> str:
        """Provider protocol type: 'oidc' or 'saml'."""

    @abstractmethod
    async def build_authorization_request(
        self,
        callback_url: str,
        state: str,
    ) -> AuthorizationRequest:
        """Build the IDP authorization URL.

        Args:
            callback_url: Terrapod's callback URL for the IDP to redirect to.
            state: Terrapod-generated state for the IDP redirect.

        Returns:
            AuthorizationRequest with the URL to redirect the user to.
        """

    @abstractmethod
    async def handle_callback(
        self,
        callback_url: str,
        **kwargs: Any,
    ) -> AuthenticatedIdentity:
        """Handle the IDP callback and extract identity.

        For OIDC: kwargs includes 'code' and 'state'.
        For SAML: kwargs includes 'saml_response'.

        Args:
            callback_url: The callback URL used in the original request.
            **kwargs: Protocol-specific callback parameters.

        Returns:
            AuthenticatedIdentity with the user's identity from the IDP.
        """
