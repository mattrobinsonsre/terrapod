"""Redis-backed ephemeral auth state for SSO flows.

Stores two types of state:
- auth_state: Created in /authorize, consumed in /callback (TTL 5 minutes)
- auth_code: Created in /callback, consumed in /token (TTL 60 seconds)

Both use atomic GET+DELETE for one-time consumption.
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
AUTH_CODE_TTL = 60  # 1 minute


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
    """
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
    async with redis.pipeline(transaction=True) as pipe:
        pipe.get(key)
        pipe.delete(key)
        results = await pipe.execute()

    data = results[0]
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

    async with redis.pipeline(transaction=True) as pipe:
        pipe.get(key)
        pipe.delete(key)
        results = await pipe.execute()

    data = results[0]
    if data is None:
        logger.warning("Auth code not found or expired")
        return None

    parsed = json.loads(data)
    return AuthCode(**parsed)
