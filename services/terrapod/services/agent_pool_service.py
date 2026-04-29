"""Agent pool, join token, and listener management service."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.ca import (
    get_ca,
    get_certificate_fingerprint,
    serialize_certificate,
    serialize_private_key,
)
from terrapod.config import settings
from terrapod.db.models import AgentPool, AgentPoolToken, utc_now
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client

logger = get_logger(__name__)


async def create_pool(
    db: AsyncSession,
    name: str,
    description: str = "",
    labels: dict | None = None,
    owner_email: str | None = None,
) -> AgentPool:
    """Create a new agent pool."""
    pool = AgentPool(
        name=name,
        description=description,
        labels=labels or {},
        owner_email=owner_email,
    )
    db.add(pool)
    await db.flush()
    return pool


async def get_pool(db: AsyncSession, pool_id: uuid.UUID) -> AgentPool | None:
    """Get an agent pool by ID."""
    result = await db.execute(select(AgentPool).where(AgentPool.id == pool_id))
    return result.scalar_one_or_none()


async def get_pool_by_name(db: AsyncSession, name: str) -> AgentPool | None:
    """Get an agent pool by name."""
    result = await db.execute(select(AgentPool).where(AgentPool.name == name))
    return result.scalar_one_or_none()


async def list_pools(db: AsyncSession) -> list[AgentPool]:
    """List all agent pools."""
    result = await db.execute(select(AgentPool).order_by(AgentPool.name))
    return list(result.scalars().all())


async def update_pool(
    db: AsyncSession,
    pool: AgentPool,
    name: str | None = None,
    description: str | None = None,
    labels: dict | None = None,
    owner_email: str | None = None,
) -> AgentPool:
    """Update an agent pool."""
    if name is not None:
        pool.name = name
    if description is not None:
        pool.description = description
    if labels is not None:
        pool.labels = labels
    if owner_email is not None:
        pool.owner_email = owner_email or None  # empty string clears
    await db.flush()
    return pool


async def delete_pool(db: AsyncSession, pool: AgentPool) -> None:
    """Delete an agent pool."""
    await db.delete(pool)
    await db.flush()


# --- Join Tokens ---


def generate_join_token() -> tuple[str, str]:
    """Generate a join token. Returns (raw_token, sha256_hash)."""
    raw = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, token_hash


_UNSET = object()


async def create_pool_token(
    db: AsyncSession,
    pool_id: uuid.UUID,
    description: str,
    created_by: str,
    expires_at=_UNSET,
    max_uses: int | None | object = _UNSET,
) -> tuple[AgentPoolToken, str]:
    """Create a join token for an agent pool. Returns (token_record, raw_token).

    `expires_at` and `max_uses` default to the values from
    `settings.agent_pools.default_join_token_*` when the caller does not
    pass them explicitly. Pass `None` to opt this token out of the limit
    (unlimited uses or no expiry). The sentinel default lets us
    distinguish "caller did not specify" from "caller wants unlimited".
    """
    raw_token, token_hash = generate_join_token()

    if expires_at is _UNSET:
        ttl = settings.agent_pools.default_join_token_ttl_seconds
        expires_at = (datetime.now(UTC) + timedelta(seconds=ttl)) if ttl else None
    if max_uses is _UNSET:
        max_uses = settings.agent_pools.default_join_token_max_uses

    token = AgentPoolToken(
        pool_id=pool_id,
        token_hash=token_hash,
        description=description,
        created_by=created_by,
        expires_at=expires_at,
        max_uses=max_uses,
    )
    db.add(token)
    await db.flush()
    return token, raw_token


async def validate_join_token(db: AsyncSession, raw_token: str) -> AgentPoolToken | None:
    """Validate a join token. Returns the token record if valid, None otherwise."""
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    result = await db.execute(select(AgentPoolToken).where(AgentPoolToken.token_hash == token_hash))
    token = result.scalar_one_or_none()
    if token is None:
        return None

    # Check revoked
    if token.is_revoked:
        return None

    # Check expiry
    if token.expires_at and utc_now() > token.expires_at:
        return None

    # Check max uses
    if token.max_uses is not None and token.use_count >= token.max_uses:
        return None

    return token


async def list_pool_tokens(db: AsyncSession, pool_id: uuid.UUID) -> list[AgentPoolToken]:
    """List all tokens for an agent pool."""
    result = await db.execute(
        select(AgentPoolToken)
        .where(AgentPoolToken.pool_id == pool_id)
        .order_by(AgentPoolToken.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke_pool_token(db: AsyncSession, token: AgentPoolToken) -> None:
    """Revoke a join token."""
    token.is_revoked = True
    await db.flush()


async def delete_pool_token(db: AsyncSession, token: AgentPoolToken) -> None:
    """Delete a join token."""
    await db.delete(token)
    await db.flush()


# --- Listeners (Redis-backed, auto-expiring) ---
#
# Listener data is ephemeral — stored in Redis with TTL, not PostgreSQL.
# Keys:
#   tp:listener:{id}                       HASH   300s TTL  — all listener data
#   tp:pool_listeners:{pid}                SET    no TTL    — pool → listener index (lazily cleaned)
#   tp:listener_name:{name}                STRING 300s TTL  — name → ID lookup
#   tp:listener_pod:{id}:{pod_name}        STRING 180s TTL  — per-pod heartbeat presence (replica count)
#
# Why a separate per-pod key family: in v0.19.0 a listener Deployment shares one
# identity (one tp:listener:{id} hash) across replicas, so the hash itself can't
# tell us how many pods are running. Each pod's heartbeat refreshes its own
# tp:listener_pod:{id}:{pod_name} key (TTL = 3x heartbeat interval); replica
# count is the number of those keys still alive.
LISTENER_TTL = 300  # seconds (refreshed on every heartbeat)
LISTENER_POD_TTL = 180  # seconds — 3x heartbeat interval, tolerates 2 missed beats
_LISTENER_PREFIX = "tp:listener:"
_POOL_LISTENERS_PREFIX = "tp:pool_listeners:"
_LISTENER_NAME_PREFIX = "tp:listener_name:"
_LISTENER_POD_PREFIX = "tp:listener_pod:"


async def join_listener(
    pool: AgentPool,
    token: AgentPoolToken,
    name: str,
    db: AsyncSession,
) -> dict:
    """Register or re-register a listener via join token exchange.

    If a listener with the same name already exists (re-join after restart),
    the existing hash is updated with a fresh certificate.

    Returns dict with listener_id, certificate PEM, private key PEM, CA cert PEM.
    """
    ca = get_ca()
    redis = get_redis_client()

    # Issue certificate
    cert, private_key = ca.issue_listener_certificate(
        name,
        pool.name,
        ttl_seconds=settings.agent_pools.listener_cert_ttl_seconds,
    )
    fingerprint = get_certificate_fingerprint(cert)
    now = datetime.now(UTC).isoformat()

    # Check if listener already exists (re-join after restart)
    existing_id = await redis.get(f"{_LISTENER_NAME_PREFIX}{name}")
    if existing_id:
        listener_id = existing_id
        # Update existing hash with fresh cert
        await redis.hset(
            f"{_LISTENER_PREFIX}{listener_id}",
            mapping={
                "pool_id": str(pool.id),
                "certificate_fingerprint": fingerprint,
                "certificate_expires_at": cert.not_valid_after_utc.isoformat(),
                "status": "online",
                "last_heartbeat": now,
            },
        )
        await redis.expire(f"{_LISTENER_PREFIX}{listener_id}", LISTENER_TTL)
        await redis.expire(f"{_LISTENER_NAME_PREFIX}{name}", LISTENER_TTL)
        # Ensure pool set membership (may have changed pools)
        await redis.sadd(f"{_POOL_LISTENERS_PREFIX}{pool.id}", listener_id)
        logger.info(
            "Listener re-joined (updated existing)",
            listener=name,
            pool=pool.name,
            fingerprint=fingerprint[:16],
        )
    else:
        listener_id = str(uuid.uuid4())
        await redis.hset(
            f"{_LISTENER_PREFIX}{listener_id}",
            mapping={
                "name": name,
                "pool_id": str(pool.id),
                "certificate_fingerprint": fingerprint,
                "certificate_expires_at": cert.not_valid_after_utc.isoformat(),
                "status": "online",
                "capacity": "10",
                "active_runs": "0",
                "last_heartbeat": now,
                "created_at": now,
            },
        )
        await redis.expire(f"{_LISTENER_PREFIX}{listener_id}", LISTENER_TTL)
        # Name → ID lookup
        await redis.setex(f"{_LISTENER_NAME_PREFIX}{name}", LISTENER_TTL, listener_id)
        # Pool index
        await redis.sadd(f"{_POOL_LISTENERS_PREFIX}{pool.id}", listener_id)
        logger.info(
            "Listener joined",
            listener=name,
            pool=pool.name,
            fingerprint=fingerprint[:16],
        )

    # Increment token use count (still DB-backed)
    token.use_count += 1
    await db.flush()

    return {
        "listener_id": listener_id,
        "certificate": serialize_certificate(cert).decode(),
        "private_key": serialize_private_key(private_key).decode(),
        "ca_certificate": ca.ca_cert_pem,
    }


async def get_listener(listener_id: uuid.UUID) -> dict | None:
    """Get a listener by ID. Returns dict of hash fields or None if expired."""
    redis = get_redis_client()
    data = await redis.hgetall(f"{_LISTENER_PREFIX}{listener_id}")
    if not data:
        return None
    data["id"] = str(listener_id)
    return data


async def get_listener_by_name(name: str) -> dict | None:
    """Get a listener by name. Returns dict of hash fields or None if expired."""
    redis = get_redis_client()
    listener_id = await redis.get(f"{_LISTENER_NAME_PREFIX}{name}")
    if not listener_id:
        return None
    data = await redis.hgetall(f"{_LISTENER_PREFIX}{listener_id}")
    if not data:
        # Name key exists but hash expired — clean up stale name key
        await redis.delete(f"{_LISTENER_NAME_PREFIX}{name}")
        return None
    data["id"] = listener_id
    return data


async def list_listeners(pool_id: uuid.UUID) -> list[dict]:
    """List all listeners for an agent pool. Lazily cleans expired entries."""
    redis = get_redis_client()
    set_key = f"{_POOL_LISTENERS_PREFIX}{pool_id}"
    member_ids = await redis.smembers(set_key)
    if not member_ids:
        return []

    # Pipeline HGETALL for all members
    pipe = redis.pipeline(transaction=False)
    id_list = list(member_ids)
    for lid in id_list:
        pipe.hgetall(f"{_LISTENER_PREFIX}{lid}")
    results = await pipe.execute()

    listeners = []
    stale_ids = []
    for lid, data in zip(id_list, results, strict=True):
        if not data:
            stale_ids.append(lid)
            continue
        data["id"] = lid
        listeners.append(data)

    # Lazily remove expired entries from the pool set
    if stale_ids:
        await redis.srem(set_key, *stale_ids)

    # Sort by name for consistent ordering
    listeners.sort(key=lambda d: d.get("name", ""))
    return listeners


async def delete_listener(listener_id: str, name: str, pool_id: str) -> None:
    """Delete a listener's Redis keys.

    Includes lazy cleanup of any per-pod presence keys so admin-driven
    listener deletion doesn't leave stragglers that could otherwise survive
    until the 180s TTL on each pod key.

    Race window: a pod can heartbeat between the SCAN+DELETE pass below and
    the listener-hash deletion in the pipeline, leaving an orphan per-pod
    key referencing a now-deleted listener. The orphan self-heals after at
    most 180s (its own TTL) and isn't visible anywhere — count_listener_replicas
    is only ever called for a listener that's still in the pool index, so the
    leak is invisible to operators. Not worth a Lua script to close.
    """
    redis = get_redis_client()
    # Per-pod keys live under a different prefix → can't go in the same pipeline
    # under cluster mode. Drain them first.
    pod_pattern = f"{_LISTENER_POD_PREFIX}{listener_id}:*"
    async for pod_key in redis.scan_iter(match=pod_pattern, count=100):
        await redis.delete(pod_key)

    pipe = redis.pipeline(transaction=False)
    pipe.delete(f"{_LISTENER_PREFIX}{listener_id}")
    pipe.delete(f"{_LISTENER_NAME_PREFIX}{name}")
    pipe.srem(f"{_POOL_LISTENERS_PREFIX}{pool_id}", listener_id)
    await pipe.execute()


async def delete_pool_listeners(pool_id: uuid.UUID) -> None:
    """Delete all listener Redis keys for a pool. Called on pool deletion."""
    redis = get_redis_client()
    set_key = f"{_POOL_LISTENERS_PREFIX}{pool_id}"
    member_ids = await redis.smembers(set_key)

    if member_ids:
        # Per-pod keys must be drained outside the pipeline (different prefix
        # → different cluster slot, can't share a pipeline with the listener
        # hash deletes).
        for lid in member_ids:
            pod_pattern = f"{_LISTENER_POD_PREFIX}{lid}:*"
            async for pod_key in redis.scan_iter(match=pod_pattern, count=100):
                await redis.delete(pod_key)

        pipe = redis.pipeline(transaction=False)
        for lid in member_ids:
            # Get name for cleanup before deleting
            name = await redis.hget(f"{_LISTENER_PREFIX}{lid}", "name")
            pipe.delete(f"{_LISTENER_PREFIX}{lid}")
            if name:
                pipe.delete(f"{_LISTENER_NAME_PREFIX}{name}")
        pipe.delete(set_key)
        await pipe.execute()
    else:
        await redis.delete(set_key)


async def renew_listener_certificate(
    listener_id: str,
    listener_name: str,
    pool: AgentPool,
) -> dict:
    """Renew a listener's certificate. Updates fingerprint and expiry in Redis."""
    ca = get_ca()
    redis = get_redis_client()
    cert, private_key = ca.issue_listener_certificate(
        listener_name,
        pool.name,
        ttl_seconds=settings.agent_pools.listener_cert_ttl_seconds,
    )
    fingerprint = get_certificate_fingerprint(cert)

    key = f"{_LISTENER_PREFIX}{listener_id}"
    await redis.hset(
        key,
        mapping={
            "certificate_fingerprint": fingerprint,
            "certificate_expires_at": cert.not_valid_after_utc.isoformat(),
        },
    )
    # Don't reset TTL here — heartbeat handles that

    logger.info(
        "Renewed listener certificate",
        listener=listener_name,
        fingerprint=fingerprint[:16],
    )

    return {
        "certificate": serialize_certificate(cert).decode(),
        "private_key": serialize_private_key(private_key).decode(),
        "ca_certificate": ca.ca_cert_pem,
    }


async def heartbeat_listener(
    listener_id: str,
    name: str,
    pod_name: str | None = None,
    **fields: str,
) -> None:
    """Refresh listener TTL and update runtime fields.

    Called by the heartbeat endpoint. Refreshes TTL on the listener hash and
    name lookup keys, merges any updated runtime fields (status, capacity,
    active_runs), and — if `pod_name` is provided — refreshes a per-pod
    presence key used to compute replica count.

    `tracks_pods=1` is set on the listener hash whenever a heartbeat carries
    pod_name. This flag lets the API distinguish "this listener is on a
    post-0.19.0 image, replica-count is authoritative" from "this listener
    is on an older image, replica-count would mislead". Once set it stays set
    for the life of the hash — older clients downstream of an upgrade can't
    sneak in and turn off tracking.
    """
    redis = get_redis_client()
    now = datetime.now(UTC).isoformat()
    updates: dict[str, str] = {"last_heartbeat": now, "status": "online", **fields}
    if pod_name:
        updates["tracks_pods"] = "1"

    # Pipeline writes that target tp:listener:* and tp:listener_name:*. These are
    # different prefixes (different cluster slots), so transaction must stay False.
    pipe = redis.pipeline(transaction=False)
    pipe.hset(f"{_LISTENER_PREFIX}{listener_id}", mapping=updates)
    pipe.expire(f"{_LISTENER_PREFIX}{listener_id}", LISTENER_TTL)
    pipe.expire(f"{_LISTENER_NAME_PREFIX}{name}", LISTENER_TTL)
    if pod_name:
        pipe.set(
            f"{_LISTENER_POD_PREFIX}{listener_id}:{pod_name}",
            now,
            ex=LISTENER_POD_TTL,
        )
    await pipe.execute()


async def count_listener_replicas(listener_id: str) -> int:
    """Return the number of pods currently heartbeating for this listener.

    Counts live `tp:listener_pod:{listener_id}:*` keys. If no pods have ever
    sent a heartbeat with `pod_name` (e.g. older listener still on a pre-0.19.0
    image), returns 0 — caller should fall back to displaying "—".
    """
    redis = get_redis_client()
    pattern = f"{_LISTENER_POD_PREFIX}{listener_id}:*"
    count = 0
    async for _ in redis.scan_iter(match=pattern, count=100):
        count += 1
    return count
