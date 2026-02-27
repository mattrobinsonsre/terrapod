"""Local identity provider connector.

Implements the SSOConnector interface for Terrapod's built-in password auth.
Instead of redirecting to an external IDP, redirects to a Terrapod-hosted login
form. The handle_callback() method validates email + password against the
database and returns an AuthenticatedIdentity just like any external connector.

Returns empty groups — role resolution (including internal role_assignments
lookup) is handled by process_login() for all providers uniformly.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.passwords import verify_password
from terrapod.auth.sso import AuthenticatedIdentity, AuthorizationRequest, SSOConnector
from terrapod.config import settings
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


class LocalConnector(SSOConnector):
    """Local password authentication as an SSOConnector.

    build_authorization_request() returns a redirect to Terrapod's own login form.
    handle_callback() validates credentials and produces an AuthenticatedIdentity.
    """

    @property
    def name(self) -> str:
        return "local"

    @property
    def provider_type(self) -> str:
        return "local"

    async def build_authorization_request(
        self,
        callback_url: str,
        state: str,
    ) -> AuthorizationRequest:
        """Build redirect URL to the Web UI login page.

        The Web UI detects the cli_state parameter and renders a form that
        POSTs to /api/v2/auth/local/login, which validates credentials and
        redirects back to the CLI's localhost callback.
        """
        login_url = f"{settings.auth.callback_base_url}/login?cli_state={state}"
        return AuthorizationRequest(
            authorize_url=login_url,
            state=state,
        )

    async def handle_callback(
        self,
        callback_url: str,
        **kwargs: Any,
    ) -> AuthenticatedIdentity:
        """Validate email + password and return an AuthenticatedIdentity.

        kwargs must include:
        - db: AsyncSession
        - email: str
        - password: str
        """
        from terrapod.db.models import User

        db: AsyncSession = kwargs["db"]
        email: str = kwargs["email"]
        password: str = kwargs["password"]

        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if not user or not user.password_hash:
            raise ValueError("Invalid credentials")

        if not verify_password(password, user.password_hash):
            raise ValueError("Invalid credentials")

        if not user.is_active:
            raise ValueError("User account is disabled")

        logger.info("Local authentication successful", email=email)

        # Return empty groups — process_login() handles role resolution
        # for all providers uniformly (including internal role_assignments).
        return AuthenticatedIdentity(
            provider_name="local",
            subject=email,
            email=email,
            display_name=user.display_name,
            groups=[],
            raw_claims={"sub": email, "auth_method": "password"},
        )
