"""Redis-backed session management.

Sessions are the primary authentication mechanism for web UI requests.
Clients receive an opaque session token (not a JWT) and include it in
the Authorization header. The server validates by looking up the session
in Redis, enabling immediate revocation.
"""

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from terrapod.config import settings
from terrapod.db.models import utc_now
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client

logger = get_logger(__name__)

SESSION_PREFIX = "tp:session:"
USER_SESSIONS_PREFIX = "tp:user_sessions:"


def _session_ttl() -> int:
    """Session TTL in seconds from config."""
    return settings.auth.session_ttl_hours * 3600


@dataclass
class Session:
    """Server-side session state stored in Redis."""

    email: str
    display_name: str | None
    roles: list[str]
    provider_name: str
    created_at: str  # ISO 8601
    expires_at: str  # ISO 8601
    last_active_at: str  # ISO 8601

    # Token is not stored in Redis — it's the key, not the value.
    token: str = field(default="", repr=False)


def generate_session_token() -> str:
    """Generate a cryptographically random session token."""
    return secrets.token_urlsafe(32)


async def create_session(
    email: str,
    display_name: str | None,
    roles: list[str],
    provider_name: str,
    max_ttl: int | None = None,
) -> Session:
    """Create a new session in Redis. Returns the Session with its token.

    Args:
        max_ttl: Optional maximum TTL in seconds. When set and shorter than
                 the configured session TTL, caps the session lifetime (e.g.,
                 when the IDP id_token expires sooner than our default).
    """
    redis = get_redis_client()
    token = generate_session_token()
    ttl = _session_ttl()
    if max_ttl is not None and 0 < max_ttl < ttl:
        ttl = max_ttl
    now = utc_now()
    expires_at = now + timedelta(seconds=ttl)

    session = Session(
        email=email,
        display_name=display_name,
        roles=roles,
        provider_name=provider_name,
        created_at=now.isoformat(),
        expires_at=expires_at.isoformat(),
        last_active_at=now.isoformat(),
        token=token,
    )

    # Store session data (exclude token — it's the key)
    data = asdict(session)
    data.pop("token")

    session_key = SESSION_PREFIX + token
    user_key = USER_SESSIONS_PREFIX + email

    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(session_key, json.dumps(data), ex=ttl)
        pipe.sadd(user_key, token)
        pipe.expire(user_key, ttl)
        await pipe.execute()

    logger.info("Session created", email=email, provider=provider_name)
    return session


async def get_session(token: str) -> Session | None:
    """Look up a session by token. Returns None if not found or expired."""
    redis = get_redis_client()
    data = await redis.get(SESSION_PREFIX + token)
    if data is None:
        return None

    parsed = json.loads(data)
    return Session(token=token, **parsed)


# Minimum interval between session TTL refreshes (seconds).
SESSION_REFRESH_INTERVAL = 300  # 5 minutes


async def refresh_session(token: str, session: Session) -> None:
    """Extend session TTL on activity (sliding window).

    Called by get_current_session when last_active_at is older than
    SESSION_REFRESH_INTERVAL. Updates last_active_at, recalculates
    expires_at, and resets the Redis TTL.
    """
    redis = get_redis_client()
    ttl = _session_ttl()
    now = utc_now()

    session_key = SESSION_PREFIX + token
    raw = await redis.get(session_key)
    if raw is None:
        return  # Session vanished between check and refresh — harmless

    data = json.loads(raw)
    data["last_active_at"] = now.isoformat()
    data["expires_at"] = (now + timedelta(seconds=ttl)).isoformat()

    user_key = USER_SESSIONS_PREFIX + session.email

    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(session_key, json.dumps(data), ex=ttl)
        pipe.expire(user_key, ttl)
        await pipe.execute()


def _should_refresh_session(session: Session) -> bool:
    """Check if enough time has passed since last refresh."""
    try:
        last_active = datetime.fromisoformat(session.last_active_at)
        return (utc_now() - last_active).total_seconds() > SESSION_REFRESH_INTERVAL
    except (ValueError, TypeError):
        return True  # If we can't parse, refresh to be safe


async def revoke_session(token: str) -> bool:
    """Revoke a session by deleting it from Redis.

    Returns True if the session existed, False if it was already gone.
    """
    redis = get_redis_client()
    session_key = SESSION_PREFIX + token

    # Get the session first to find the email for cleanup
    data = await redis.get(session_key)

    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(session_key)
        if data is not None:
            parsed = json.loads(data)
            user_key = USER_SESSIONS_PREFIX + parsed["email"]
            pipe.srem(user_key, token)
        results = await pipe.execute()

    deleted = results[0] > 0
    if deleted:
        logger.info("Session revoked")
    return deleted


async def list_user_sessions(email: str) -> list[Session]:
    """List all active sessions for a user.

    Cleans up stale entries (tokens that have expired from Redis but
    remain in the user's session set).
    """
    redis = get_redis_client()
    user_key = USER_SESSIONS_PREFIX + email

    tokens = await redis.smembers(user_key)
    if not tokens:
        return []

    sessions = []
    stale_tokens = []

    for token_bytes in tokens:
        token = token_bytes if isinstance(token_bytes, str) else token_bytes.decode()
        data = await redis.get(SESSION_PREFIX + token)
        if data is None:
            stale_tokens.append(token)
            continue
        parsed = json.loads(data)
        sessions.append(Session(token=token, **parsed))

    # Clean up stale entries
    if stale_tokens:
        await redis.srem(user_key, *stale_tokens)

    return sessions


async def list_all_sessions() -> list[Session]:
    """List all active sessions across all users.

    Uses SCAN to iterate keys matching the session prefix.
    """
    redis = get_redis_client()
    sessions: list[Session] = []

    async for key in redis.scan_iter(match=f"{SESSION_PREFIX}*", count=100):
        data = await redis.get(key)
        if data is None:
            continue
        key_str = key if isinstance(key, str) else key.decode()
        token = key_str[len(SESSION_PREFIX) :]
        parsed = json.loads(data)
        sessions.append(Session(token=token, **parsed))

    return sessions


async def revoke_all_user_sessions(email: str) -> int:
    """Revoke all sessions for a user. Returns count of sessions revoked."""
    redis = get_redis_client()
    user_key = USER_SESSIONS_PREFIX + email

    tokens = await redis.smembers(user_key)
    if not tokens:
        return 0

    async with redis.pipeline(transaction=True) as pipe:
        for token_bytes in tokens:
            token = token_bytes if isinstance(token_bytes, str) else token_bytes.decode()
            pipe.delete(SESSION_PREFIX + token)
        pipe.delete(user_key)
        results = await pipe.execute()

    # Count actual deletions (exclude the final delete of the set itself)
    count = sum(1 for r in results[:-1] if r > 0)
    logger.info("Revoked all sessions for user", email=email, count=count)
    return count
