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

from terrapod.auth.sso import AuthenticatedIdentity, AuthorizationRequest, SSOConnector
from terrapod.config import OIDCProviderConfig
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


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

        params = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "redirect_uri": callback_url,
            "scope": " ".join(self._config.scopes),
            "state": state,
            "nonce": nonce,
        }

        # Include audience if configured (requests API-scoped access token)
        if self._config.audience:
            params["audience"] = self._config.audience

        authorize_url = f"{authorization_endpoint}?{urlencode(params)}"
        return AuthorizationRequest(
            authorize_url=authorize_url,
            state=state,
            nonce=nonce,
        )

    async def handle_callback(
        self,
        callback_url: str,
        **kwargs: Any,
    ) -> AuthenticatedIdentity:
        """Exchange authorization code for tokens and extract identity."""
        code = kwargs["code"]

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
            claims.validate()
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
        groups = _strip_role_prefixes(groups, self._config.role_prefixes)

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
            at_claims.validate()
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


def _strip_role_prefixes(groups: list[str], prefixes: list[str]) -> list[str]:
    """Strip configured prefixes from group names to derive role names.

    e.g., with prefix 'terrapod-', group 'terrapod-admin' becomes role 'admin'.
    Groups without a matching prefix are passed through unchanged.
    """
    if not prefixes:
        return groups

    result = []
    for g in groups:
        stripped = False
        for prefix in prefixes:
            if g.startswith(prefix):
                result.append(g[len(prefix) :])
                stripped = True
                break
        if not stripped:
            result.append(g)
    return result
