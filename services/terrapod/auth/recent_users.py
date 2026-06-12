"""Redis-backed recent user tracking for admin UX.

Tracks recently-seen (provider, email) pairs in Redis with a 7-day TTL.
Set on each login. Used by admin UI for autocomplete/suggestions when
assigning roles to users.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client

logger = get_logger(__name__)

RECENT_USER_PREFIX = "tp:recent_user:"
RECENT_USER_TTL = 604800  # 7 days in seconds

# Provider-agnostic "last interactive login" marker, keyed by email only
# (#495). Distinct from RECENT_USER (which is provider-scoped, for admin
# autocomplete): this one is load-bearing for auth — a user-bound token is
# rejected once this marker has expired (the owner hasn't logged in within
# the idle window). Set on every login in `process_login`; its TTL is the
# configured idle window. Single-key GET/SET only — never pipelined across
# prefixes (cluster cross-slot rule).
USER_SEEN_PREFIX = "tp:user_seen:"


async def mark_user_seen(email: str, ttl_seconds: int) -> None:
    """Refresh the idle-login marker for an identity (#495).

    Called on every interactive login. `ttl_seconds` is the idle window
    (``bound_token_idle_days`` × 86400). A non-positive TTL means idle
    rejection is disabled, so there's nothing to mark.
    """
    if ttl_seconds <= 0 or not email:
        return
    redis = get_redis_client()
    await redis.set(f"{USER_SEEN_PREFIX}{email}", "1", ex=ttl_seconds)


async def user_seen_within_window(email: str) -> bool:
    """True if the identity has logged in within the idle window (#495)."""
    if not email:
        return False
    redis = get_redis_client()
    return (await redis.get(f"{USER_SEEN_PREFIX}{email}")) is not None


@dataclass
class RecentUser:
    """A recently-seen user identity."""

    provider_name: str
    email: str
    display_name: str | None
    last_seen: str


async def record_recent_user(
    provider_name: str,
    email: str,
    display_name: str | None,
) -> None:
    """Record a user login in Redis with 7-day TTL."""
    redis = get_redis_client()
    key = f"{RECENT_USER_PREFIX}{provider_name}:{email}"
    value = json.dumps(
        {
            "provider_name": provider_name,
            "email": email,
            "display_name": display_name,
            "last_seen": datetime.now(UTC).isoformat(),
        }
    )
    await redis.set(key, value, ex=RECENT_USER_TTL)


async def list_recent_users() -> list[RecentUser]:
    """List all recently-seen users from Redis.

    Uses SCAN to iterate keys matching the prefix.
    """
    redis = get_redis_client()
    users: list[RecentUser] = []

    async for key in redis.scan_iter(match=f"{RECENT_USER_PREFIX}*", count=100):
        data = await redis.get(key)
        if data is None:
            continue
        parsed = json.loads(data)
        users.append(
            RecentUser(
                provider_name=parsed["provider_name"],
                email=parsed["email"],
                display_name=parsed.get("display_name"),
                last_seen=parsed["last_seen"],
            )
        )

    # Sort by last_seen descending (most recent first)
    users.sort(key=lambda u: u.last_seen, reverse=True)
    return users
