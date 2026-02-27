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
