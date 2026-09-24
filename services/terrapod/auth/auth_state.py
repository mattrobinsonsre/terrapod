"""Redis-backed ephemeral auth state for SSO flows.

Stores two types of state:
- auth_state: Created in /authorize, consumed in /callback (TTL 5 minutes)
- auth_code: Created in /callback, consumed in /token (TTL 5 minutes)

Both consume with GETDEL, which is genuinely atomic — a pipeline
without MULTI/EXEC is batching and lets two clients redeem the same
one-time value.
"""

import json
import secrets
from dataclasses import asdict, dataclass

from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client

logger = get_logger(__name__)

AUTH_STATE_PREFIX = "tp:auth_state:"
AUTH_CODE_PREFIX = "tp:auth_code:"
AUTH_STATE_TTL = 300  # 5 minutes

# The window the CLI has to redeem the code the browser just handed it.
#
# This was 60s, and the browser hand-off page waited 60s before offering its
# manual fallback -- so a user who needed that fallback was always given a code
# that had already expired. The manual path could not work at all, and on
# Safari it is the ONLY path: WebKit blocks the page's mixed-content fetch to
# http://127.0.0.1 that Chromium permits.
#
# 5 minutes is well inside RFC 6749 s4.1.2's recommended 10-minute maximum for
# an authorization code, and the code's real protections are unchanged: it is
# single-use (GETDEL below) and bound to the PKCE verifier, which is checked
# before anything is issued. Matching AUTH_STATE_TTL keeps the two legs of the
# same login consistent.
#
# TWO consumers, not one. `routers/auth.py` calls `store_auth_code` for the
# web SESSION flow as well, so that code's lifetime moved too -- it sits in
# the address bar and history at `/auth/callback?code=...` for five times as
# long now. Same risk class (PKCE is verified on that path too, at
# `routers/auth.py`, and the code is equally single-use), and deliberate
# rather than incidental: the CLI hand-off is what forced the change, but the
# constant is shared and the session flow was never going to be left on a
# different number.
AUTH_CODE_TTL = 300  # 5 minutes


@dataclass
class AuthState:
    """State stored between /authorize and /callback."""

    provider_name: str
    client_redirect_uri: str
    client_state: str
    code_challenge: str
    code_challenge_method: str
    idp_state: str
    nonce: str | None = None
    idp_code_verifier: str | None = None
    # "session" for web UI, "api_token" for terraform login
    credential_type: str = "session"


@dataclass
class AuthCode:
    """State stored between /callback and /token."""

    email: str
    roles: list[str]
    provider_name: str
    code_challenge: str
    code_challenge_method: str
    display_name: str | None = None
    # Maximum session TTL in seconds, set when the IDP id_token expires
    # sooner than the configured session_ttl_hours.
    max_session_ttl: int | None = None
    # "session" for web UI, "api_token" for terraform login
    credential_type: str = "session"


def generate_state() -> str:
    """Generate a cryptographically random state parameter."""
    return secrets.token_urlsafe(32)


def generate_code() -> str:
    """Generate a cryptographically random authorization code."""
    return secrets.token_urlsafe(32)


async def store_auth_state(state: AuthState) -> str:
    """Store auth state in Redis, keyed by IDP-facing state.

    Returns the IDP state key used for lookup.

    **Validates `client_redirect_uri` here rather than in the routes.** Whatever
    is stored is later handed the authorization code, and the bug this closes
    was two routes with one missing check — validating per route would leave the
    next route to remember. Raises `InvalidRedirectURI`; callers turn it into a
    400.
    """
    from terrapod.auth.redirect_uri import validate_redirect_uri
    from terrapod.config import settings

    validate_redirect_uri(
        state.client_redirect_uri,
        credential_type=state.credential_type,
        allowed_origin=(settings.auth.callback_base_url or settings.external_url or ""),
    )

    redis = get_redis_client()
    key = AUTH_STATE_PREFIX + state.idp_state
    await redis.set(key, json.dumps(asdict(state)), ex=AUTH_STATE_TTL)
    logger.debug("Stored auth state", idp_state=state.idp_state, provider=state.provider_name)
    return state.idp_state


async def consume_auth_state(idp_state: str) -> AuthState | None:
    """Consume (get + delete) auth state. Returns None if not found or expired."""
    redis = get_redis_client()
    key = AUTH_STATE_PREFIX + idp_state

    # Atomic get-and-delete via pipeline
    # GETDEL for the same reason as consume_auth_code below: one key, and
    # "one-time" has to mean it.
    data = await redis.getdel(key)
    if data is None:
        logger.warning("Auth state not found or expired", idp_state=idp_state)
        return None

    parsed = json.loads(data)
    return AuthState(**parsed)


async def store_auth_code(code: str, auth_code: AuthCode) -> None:
    """Store a one-time auth code in Redis."""
    redis = get_redis_client()
    key = AUTH_CODE_PREFIX + code
    await redis.set(key, json.dumps(asdict(auth_code)), ex=AUTH_CODE_TTL)
    logger.debug("Stored auth code", email=auth_code.email)


async def consume_auth_code(code: str) -> AuthCode | None:
    """Consume (get + delete) an auth code. Returns None if not found or expired."""
    redis = get_redis_client()
    key = AUTH_CODE_PREFIX + code

    # GETDEL, not a pipeline (#1835). A pipeline is BATCHING, not atomicity:
    # with `transaction=False` there is no MULTI/EXEC, so nothing stops a
    # second client's GET landing between this GET and this DELETE, and both
    # redeeming the same one-time code. The docstring above and the
    # AUTH_CODE_TTL rationale both lean on "single-use", so it has to be true.
    #
    # The project's rule against `transaction=True` is about pipelines
    # spanning keys that hash to different slots in cluster mode; this is one
    # key, so it does not apply. GETDEL needs Redis 6.2+ and the project
    # requires 7+.
    data = await redis.getdel(key)
    if data is None:
        logger.warning("Auth code not found or expired")
        return None

    parsed = json.loads(data)
    return AuthCode(**parsed)
