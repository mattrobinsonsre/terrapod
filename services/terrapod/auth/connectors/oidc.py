"""OIDC identity provider connector.

Uses authlib for OIDC discovery, token exchange, and JWKS-based ID token validation.
Supports API audience for application-scoped permissions (Auth0, Okta, etc.)
and /userinfo endpoint for additional claims.
"""

import secrets
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey
from authlib.jose import jwt as authlib_jwt
from authlib.jose.errors import JoseError

from terrapod.auth.idp_groups import roles_from_idp_groups
from terrapod.auth.pkce import s256_challenge
from terrapod.auth.sso import AuthenticatedIdentity, AuthorizationRequest, SSOConnector
from terrapod.config import OIDCProviderConfig
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

#: Distinguishes "the IdP did not send email_verified" from "it sent a falsy
#: value". They mean different things and are refused under different rules, so
#: a plain ``.get(..., False)`` default would collapse them.
_CLAIM_ABSENT = object()


def _generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, S256 code_challenge) for upstream OIDC PKCE."""
    verifier = secrets.token_urlsafe(64)
    return verifier, s256_challenge(verifier)


class OIDCConnector(SSOConnector):
    """OIDC identity provider connector using authlib."""

    def __init__(self, config: OIDCProviderConfig) -> None:
        self._config = config
        self._discovery: dict[str, Any] | None = None
        self._jwks: Any | None = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def display_name(self) -> str:
        return self._config.display_name or self._config.name

    @property
    def provider_type(self) -> str:
        return "oidc"

    async def _ensure_discovery(self) -> dict[str, Any]:
        """Fetch and cache OIDC discovery document."""
        if self._discovery is not None:
            return self._discovery

        discovery_url = self._config.issuer_url.rstrip("/") + "/.well-known/openid-configuration"
        async with httpx.AsyncClient() as client:
            resp = await client.get(discovery_url)
            resp.raise_for_status()
            self._discovery = resp.json()

        logger.info("OIDC discovery loaded", provider=self.name, issuer=self._config.issuer_url)
        return self._discovery

    async def _ensure_jwks(self) -> Any:
        """Fetch and cache JWKS for token verification."""
        if self._jwks is not None:
            return self._jwks

        discovery = await self._ensure_discovery()
        jwks_uri = discovery["jwks_uri"]

        async with httpx.AsyncClient() as client:
            resp = await client.get(jwks_uri)
            resp.raise_for_status()
            self._jwks = JsonWebKey.import_key_set(resp.json())

        return self._jwks

    async def build_authorization_request(
        self,
        callback_url: str,
        state: str,
    ) -> AuthorizationRequest:
        """Build the OIDC authorization URL."""
        discovery = await self._ensure_discovery()
        authorization_endpoint = discovery["authorization_endpoint"]
        nonce = secrets.token_urlsafe(32)
        code_verifier, code_challenge = _generate_pkce_pair()

        params = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "redirect_uri": callback_url,
            "scope": " ".join(self._config.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

        # Include audience if configured (requests API-scoped access token)
        if self._config.audience:
            params["audience"] = self._config.audience

        authorize_url = f"{authorization_endpoint}?{urlencode(params)}"
        return AuthorizationRequest(
            authorize_url=authorize_url,
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
        )

    def _require_vouched_identity(
        self, subject: str, email: str, merged_claims: dict[str, Any]
    ) -> None:
        """Refuse a login whose principal the IdP has not vouched for.

        The email claim is the principal everywhere downstream: it selects role
        assignments, owns workspaces and names token owners. Three ways that goes
        wrong, all of which this refuses (GHSA-3m8x-ff8g-7x8c):

        * **No subject.** `sub` is mandatory in OIDC, so an empty one is a broken
          IdP -- and subject is the stable half of the identity, the half that
          survives the user changing their email.
        * **No email.** Every such user collapses into one empty principal that
          shares whatever is assigned to "".
        * **Email the IdP does not vouch for.** An explicit
          ``email_verified: false`` is refused unconditionally: the IdP is saying
          the address is unverified, and an attacker at any configured provider
          could otherwise set a victim's address and inherit their roles. A
          *missing* claim is the weaker case, governed by
          ``require_email_verified`` (default true, i.e. absent means unverified)
          because some IdPs verify email without sending it.

        Raises ValueError, which the callback path already surfaces as a failed
        login -- never a partial identity.
        """
        if not subject:
            raise ValueError(
                f"{self.name} returned no 'sub' claim; refusing a login with no stable subject"
            )
        if not email:
            raise ValueError(
                f"{self.name} returned no 'email' claim; refusing a login with no principal"
            )

        verified = merged_claims.get("email_verified", _CLAIM_ABSENT)
        if verified is _CLAIM_ABSENT:
            if self._config.require_email_verified:
                raise ValueError(
                    f"{self.name} did not send an 'email_verified' claim for {email!r}. "
                    "The email claim is the principal, so an unvouched address is refused. "
                    "Set require_email_verified=false for this provider only if it verifies "
                    "email without sending the claim."
                )
            return

        # Present: must be truthy. A string "false" is falsy as an assertion of
        # verification but truthy to Python, so compare against the real shapes
        # an IdP sends rather than relying on bool().
        if verified is True or (isinstance(verified, str) and verified.lower() == "true"):
            return
        raise ValueError(
            f"{self.name} reports email_verified={verified!r} for {email!r}; refusing the login"
        )

    async def handle_callback(
        self,
        callback_url: str,
        **kwargs: Any,
    ) -> AuthenticatedIdentity:
        """Exchange authorization code for tokens and extract identity."""
        code = kwargs["code"]
        code_verifier = kwargs.get("code_verifier")

        discovery = await self._ensure_discovery()
        token_endpoint = discovery["token_endpoint"]

        # Exchange code for tokens (server-to-server with client_secret)
        token_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": callback_url,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
        }
        if code_verifier:
            token_data["code_verifier"] = code_verifier

        async with httpx.AsyncClient() as client:
            resp = await client.post(token_endpoint, data=token_data)
            resp.raise_for_status()
            token_response = resp.json()

        id_token_raw = token_response.get("id_token")
        if not id_token_raw:
            raise ValueError(f"No id_token in response from {self.name}")

        access_token = token_response.get("access_token", "")

        # Validate ID token with JWKS
        discovery = await self._ensure_discovery()
        expected_issuer = discovery.get("issuer", self._config.issuer_url)
        jwks = await self._ensure_jwks()
        try:
            claims = authlib_jwt.decode(
                id_token_raw,
                jwks,
                claims_options={
                    "iss": {"essential": True, "value": expected_issuer},
                    "aud": {"essential": True, "value": self._config.client_id},
                },
            )
            claims.validate(leeway=30)
        except JoseError as e:
            raise ValueError(f"ID token validation failed for {self.name}: {e}") from e

        # Merge additional claims from /userinfo endpoint
        userinfo_claims = await self._fetch_userinfo(access_token)
        merged_claims = {**dict(claims), **userinfo_claims}

        # Extract permissions from the access token if audience is configured
        permissions = self._extract_access_token_permissions(access_token, jwks, expected_issuer)

        # Extract identity from merged claims
        subject = merged_claims.get("sub", "")
        email = merged_claims.get("email", "")
        self._require_vouched_identity(subject, email, merged_claims)
        display_name = merged_claims.get("name") or merged_claims.get("preferred_username")

        # Groups from the configured claim name (ID token or userinfo)
        groups = merged_claims.get(self._config.groups_claim, [])
        if isinstance(groups, str):
            groups = [groups]

        # Merge API permissions into groups
        if permissions:
            groups = list(set(groups) | set(permissions))
            logger.info(
                "Extracted API permissions from access token",
                provider=self.name,
                permissions=permissions,
            )

        # Strip configured role prefixes
        groups = roles_from_idp_groups(groups, self._config.role_prefixes, provider=self.name)

        # Extract id_token expiry to cap session TTL
        id_token_expires_at = None
        id_token_exp = claims.get("exp")
        if id_token_exp is not None:
            try:
                id_token_expires_at = datetime.fromtimestamp(int(id_token_exp), tz=UTC)
            except (ValueError, TypeError, OSError):
                logger.warning(
                    "Failed to parse id_token exp claim", provider=self.name, exp=id_token_exp
                )

        logger.info(
            "OIDC authentication successful",
            provider=self.name,
            subject=subject,
            email=email,
            groups=groups,
        )

        return AuthenticatedIdentity(
            provider_name=self.name,
            subject=subject,
            email=email,
            display_name=display_name,
            groups=groups,
            raw_claims=merged_claims,
            id_token_expires_at=id_token_expires_at,
        )

    async def _fetch_userinfo(self, access_token: str) -> dict[str, Any]:
        """Call the /userinfo endpoint and return claims.

        Returns empty dict on failure (non-fatal).
        """
        if not access_token:
            return {}

        discovery = await self._ensure_discovery()
        userinfo_endpoint = discovery.get("userinfo_endpoint")
        if not userinfo_endpoint:
            return {}

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                resp.raise_for_status()
                userinfo = resp.json()
                logger.debug("Fetched userinfo", provider=self.name, claims=list(userinfo.keys()))
                return userinfo
        except Exception:
            logger.warning("Failed to fetch userinfo", provider=self.name, exc_info=True)
            return {}

    def _extract_access_token_permissions(
        self,
        access_token: str,
        jwks: Any,
        expected_issuer: str,
    ) -> list[str]:
        """Decode the access token JWT and extract the permissions array.

        When an API audience is configured, the access token is a JWT containing
        application-scoped permissions. Returns empty list if no audience is
        configured or if the token isn't a JWT.
        """
        if not self._config.audience or not access_token:
            return []

        try:
            at_claims = authlib_jwt.decode(
                access_token,
                jwks,
                claims_options={
                    "iss": {"essential": True, "value": expected_issuer},
                    "aud": {"essential": True, "value": self._config.audience},
                },
            )
            at_claims.validate(leeway=30)
            permissions = at_claims.get("permissions", [])
            if isinstance(permissions, str):
                permissions = [permissions]
            return permissions
        except JoseError:
            logger.debug(
                "Access token is not a verifiable JWT (may be opaque)",
                provider=self.name,
            )
            return []
        except Exception:
            logger.warning(
                "Failed to decode access token",
                provider=self.name,
                exc_info=True,
            )
            return []
