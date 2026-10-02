"""Redis-backed session management.

Sessions are the primary authentication mechanism for web UI requests.
Clients receive an opaque session token (not a JWT) and include it in
the Authorization header. The server validates by looking up the session
in Redis, enabling immediate revocation.

**A session's roles are resolved once, at login** (`sso_service.process_login`
merges IdP groups, claims rules and the stored assignments) and then carried in
the Redis record, because two of those three sources need the IdP's token and are
gone by the next request. That is why a role change has to come to the session
rather than the session going to look for it, and this module carries both halves
(GHSA-pwrq-j4cv-w7qg):

* `revoke_user_sessions` for a reduction — a removed role, a deleted grant, a
  password reset. Nothing short of ending the session is honest there: the
  session's role list is the authorization, so leaving it in place leaves the
  access in place.
* `grant_roles_to_user_sessions` for a widening, which adds the new roles to the
  sessions that already exist instead of ending them. It is a union rather than a
  re-resolution on purpose: re-running role resolution here would see only the
  stored assignments and would silently drop every role the user holds through an
  IdP group or a claims rule.

**And `session_absolute_ttl_hours` bounds whatever both of those miss.** The
sliding window is clamped to a deadline measured from login, so a session cannot
be kept alive indefinitely by activity.
"""

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from terrapod.config import settings
from terrapod.db.models import now_utc
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client

logger = get_logger(__name__)

SESSION_PREFIX = "tp:session:"
USER_SESSIONS_PREFIX = "tp:user_sessions:"


def _session_ttl() -> int:
    """Session TTL in seconds from config."""
    return settings.auth.session_ttl_hours * 3600


def _absolute_ttl() -> int:
    """Seconds from login after which a session ends regardless of activity.

    0 means no ceiling, which is the operator explicitly asking for the old
    indefinitely-slidable behaviour.
    """
    return settings.auth.session_absolute_ttl_hours * 3600


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

    #: ISO 8601 deadline this session cannot slide past, or "" when the ceiling
    #: is disabled. Defaulted because a record written before the field existed
    #: must still parse; `absolute_deadline()` derives one from `created_at` in
    #: that case rather than treating the gap as "no ceiling".
    absolute_expires_at: str = ""

    # Token is not stored in Redis — it's the key, not the value.
    token: str = field(default="", repr=False)


def _parse_iso(value: str) -> datetime | None:
    """An aware UTC datetime, or None for anything unparseable.

    A naive value is read as UTC rather than returned as-is: every timestamp this
    module writes is aware, but comparing an aware `now` against a naive stored
    value raises, and that exception would be raised inside `get_session` — which
    every authenticated request goes through. Refusing to compare is not an
    option; refusing to crash is the point.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def absolute_deadline(session: Session) -> datetime | None:
    """When this session ends no matter what, or None if nothing caps it.

    The EARLIER of what the record says and what the configuration currently
    allows, for two reasons that pull in opposite directions:

    * a session created before this field existed carries no deadline of its
      own, and deriving one from `created_at` is what stops the sessions that
      predate the fix being the exempt ones;
    * an operator who *lowers* the ceiling means it for the sessions that are
      already open, so the stored value must not be allowed to outlast it.

    Taking the minimum satisfies both and can only ever tighten.
    """
    ttl = _absolute_ttl()
    if ttl <= 0:
        return None
    created = _parse_iso(session.created_at)
    from_config = created + timedelta(seconds=ttl) if created is not None else None
    stored = _parse_iso(session.absolute_expires_at)
    candidates = [d for d in (stored, from_config) if d is not None]
    return min(candidates) if candidates else None


def is_past_absolute_deadline(session: Session) -> bool:
    """True when the session has outlived its ceiling and must not authenticate."""
    deadline = absolute_deadline(session)
    return deadline is not None and deadline <= now_utc()


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
    now = now_utc()
    absolute_ttl = _absolute_ttl()
    absolute_expires_at = (
        (now + timedelta(seconds=absolute_ttl)).isoformat() if absolute_ttl > 0 else ""
    )
    # A ceiling shorter than the sliding window would otherwise leave a session
    # whose Redis key outlives the deadline it can never pass.
    if absolute_ttl > 0:
        ttl = min(ttl, absolute_ttl)
    expires_at = now + timedelta(seconds=ttl)

    session = Session(
        email=email,
        display_name=display_name,
        roles=roles,
        provider_name=provider_name,
        created_at=now.isoformat(),
        expires_at=expires_at.isoformat(),
        last_active_at=now.isoformat(),
        absolute_expires_at=absolute_expires_at,
        token=token,
    )

    # Store session data (exclude token — it's the key)
    data = asdict(session)
    data.pop("token")

    session_key = SESSION_PREFIX + token
    user_key = USER_SESSIONS_PREFIX + email

    async with redis.pipeline(transaction=False) as pipe:
        pipe.set(session_key, json.dumps(data), ex=ttl)
        pipe.sadd(user_key, token)
        pipe.expire(user_key, ttl)
        await pipe.execute()

    logger.info("Session created", email=email, provider=provider_name)
    return session


async def get_session(token: str) -> Session | None:
    """Look up a session by token. Returns None if not found or expired.

    The absolute deadline is enforced HERE rather than left to the Redis key's
    own TTL, because the two can disagree: an operator who lowers
    `session_absolute_ttl_hours` leaves live keys whose TTL runs past the new
    deadline, and a session created before the ceiling existed has no stored
    deadline at all. This is the one place every authenticated request passes
    through, so it is the only place the ceiling actually binds.
    """
    redis = get_redis_client()
    data = await redis.get(SESSION_PREFIX + token)
    if data is None:
        return None

    parsed = json.loads(data)
    session = Session(token=token, **parsed)
    if is_past_absolute_deadline(session):
        logger.info(
            "Session reached its absolute expiry",
            email=session.email,
            provider=session.provider_name,
        )
        await revoke_session(token)
        return None
    return session


async def get_session_ttl(token: str) -> int | None:
    """Return the session's true remaining TTL in seconds, WITHOUT sliding it.

    Reads the Redis key TTL directly so a caller (the web session-expiry
    banner, #726) can reconcile against the server's real remaining lifetime
    rather than a stale client-cached timestamp. Returns None when the key is
    gone (`-2`) or has no expiry (`-1`); a non-negative int otherwise. This is
    a pure read — it must never call refresh_session, or polling it would keep
    the session alive forever and the warning could never fire.
    """
    redis = get_redis_client()
    ttl = await redis.ttl(SESSION_PREFIX + token)
    return ttl if ttl is not None and ttl >= 0 else None


# Minimum interval between session TTL refreshes (seconds).
SESSION_REFRESH_INTERVAL = 300  # 5 minutes


async def refresh_session(token: str, session: Session) -> str:
    """Extend session TTL on activity (sliding window), clamped to the ceiling.

    Called by get_current_session when last_active_at is older than
    SESSION_REFRESH_INTERVAL. Updates last_active_at, recalculates
    expires_at, and resets the Redis TTL.

    **The clamp is what makes the ceiling real.** Re-arming the full sliding TTL
    unconditionally is how a session stays alive for as long as a browser keeps
    polling it, which is what let a stale role list outlive the change to it.
    The new expiry is never later than `absolute_deadline`, and a session already
    past it is revoked here rather than re-armed.

    Returns the new expires_at ISO 8601 timestamp.
    """
    redis = get_redis_client()
    ttl = _session_ttl()
    now = now_utc()

    session_key = SESSION_PREFIX + token
    raw = await redis.get(session_key)
    if raw is None:
        return session.expires_at  # Session vanished — return original

    data = json.loads(raw)
    # Read the deadline off the STORED record, not off the caller's snapshot:
    # another replica may have written it since, and the stored copy is the one
    # we are about to re-serialise.
    stored = Session(token=token, **data)
    deadline = absolute_deadline(stored)
    if deadline is not None:
        remaining = int((deadline - now).total_seconds())
        if remaining <= 0:
            logger.info(
                "Session reached its absolute expiry",
                email=stored.email,
                provider=stored.provider_name,
            )
            await revoke_session(token)
            return stored.expires_at
        ttl = min(ttl, remaining)

    new_expires_at = (now + timedelta(seconds=ttl)).isoformat()
    data["last_active_at"] = now.isoformat()
    data["expires_at"] = new_expires_at

    user_key = USER_SESSIONS_PREFIX + session.email

    async with redis.pipeline(transaction=False) as pipe:
        pipe.set(session_key, json.dumps(data), ex=ttl)
        pipe.expire(user_key, ttl)
        await pipe.execute()

    return new_expires_at


def _should_refresh_session(session: Session) -> bool:
    """Check if enough time has passed since last refresh."""
    try:
        last_active = datetime.fromisoformat(session.last_active_at)
        return (now_utc() - last_active).total_seconds() > SESSION_REFRESH_INTERVAL
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

    async with redis.pipeline(transaction=False) as pipe:
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
        session = Session(token=token, **parsed)
        # A session past its ceiling cannot authenticate, so listing it as
        # active would tell an admin checking whether someone is still signed in
        # exactly the wrong thing.
        if is_past_absolute_deadline(session):
            continue
        sessions.append(session)

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
        session = Session(token=token, **parsed)
        if is_past_absolute_deadline(session):
            continue
        sessions.append(session)

    return sessions


async def revoke_all_user_sessions(email: str) -> int:
    """Revoke all sessions for a user. Returns count of sessions revoked."""
    redis = get_redis_client()
    user_key = USER_SESSIONS_PREFIX + email

    tokens = await redis.smembers(user_key)
    if not tokens:
        return 0

    async with redis.pipeline(transaction=False) as pipe:
        for token_bytes in tokens:
            token = token_bytes if isinstance(token_bytes, str) else token_bytes.decode()
            pipe.delete(SESSION_PREFIX + token)
        pipe.delete(user_key)
        results = await pipe.execute()

    # Count actual deletions (exclude the final delete of the set itself)
    count = sum(1 for r in results[:-1] if r > 0)
    logger.info("Revoked all sessions for user", email=email, count=count)
    return count


def _decode_token(raw: str | bytes) -> str:
    return raw if isinstance(raw, str) else raw.decode()


async def revoke_user_sessions(email: str, provider_name: str | None = None) -> int:
    """Revoke a user's sessions, optionally only those from one SSO provider.

    Without `provider_name` this is `revoke_all_user_sessions`. With it, only the
    sessions whose own provider matches are ended, because a role assignment is
    keyed on `(provider_name, email)` and login resolves a session's roles from
    the assignments for ITS provider — so a change to one provider's grants
    cannot have made another provider's session stale, and ending it would be a
    logout nobody asked for.
    """
    if provider_name is None:
        return await revoke_all_user_sessions(email)

    redis = get_redis_client()
    user_key = USER_SESSIONS_PREFIX + email

    tokens = await redis.smembers(user_key)
    if not tokens:
        return 0

    doomed: list[str] = []
    for raw in tokens:
        token = _decode_token(raw)
        data = await redis.get(SESSION_PREFIX + token)
        if data is None:
            continue  # already gone; the stale-set sweep in list_user_sessions tidies it
        if json.loads(data).get("provider_name") == provider_name:
            doomed.append(token)

    if not doomed:
        return 0

    async with redis.pipeline(transaction=False) as pipe:
        for token in doomed:
            pipe.delete(SESSION_PREFIX + token)
        # The set is NOT deleted — it still indexes this user's sessions from
        # other providers. Cross-slot in cluster mode, hence transaction=False.
        pipe.srem(user_key, *doomed)
        results = await pipe.execute()

    count = sum(1 for r in results[:-1] if r > 0)
    logger.info(
        "Revoked sessions for user",
        email=email,
        provider=provider_name,
        count=count,
    )
    return count


async def grant_roles_to_user_sessions(
    email: str,
    roles: set[str] | frozenset[str],
    provider_name: str | None = None,
) -> int:
    """Add roles to a user's live sessions in place. Returns sessions touched.

    This is the widening half: granting access need not log anyone out. The roles
    are UNIONED into whatever the session already carries rather than recomputed,
    because the other two sources login merges — IdP groups and claims rules —
    need the IdP's token and are long gone by now. Re-resolving from the stored
    assignments alone would look like a refresh and would quietly strip every
    role the user holds through their directory.

    Each session's remaining Redis TTL is preserved, so a widening neither
    extends a session nor shortens it.
    """
    if not roles:
        return 0

    redis = get_redis_client()
    tokens = await redis.smembers(USER_SESSIONS_PREFIX + email)
    if not tokens:
        return 0

    touched = 0
    for raw in tokens:
        token = _decode_token(raw)
        session_key = SESSION_PREFIX + token
        data = await redis.get(session_key)
        if data is None:
            continue
        parsed = json.loads(data)
        if provider_name is not None and parsed.get("provider_name") != provider_name:
            continue
        merged = sorted(set(parsed.get("roles") or []) | set(roles))
        if merged == parsed.get("roles"):
            continue
        ttl = await redis.ttl(session_key)
        if ttl is None or ttl <= 0:
            # No expiry left to preserve: the key is on its way out, and writing
            # it back with a fresh TTL would resurrect it.
            continue
        parsed["roles"] = merged
        await redis.set(session_key, json.dumps(parsed), ex=ttl)
        touched += 1

    if touched:
        logger.info(
            "Granted roles to live sessions",
            email=email,
            provider=provider_name,
            roles=sorted(roles),
            sessions=touched,
        )
    return touched
