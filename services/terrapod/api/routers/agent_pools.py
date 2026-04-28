"""Agent pool, join token, listener management, and heartbeat endpoints.

UX CONTRACT: Pool/token/listener endpoints are consumed by the web frontend:
  - web/src/app/admin/agent-pools/page.tsx (pool list, create)
  - web/src/app/admin/agent-pools/[id]/page.tsx (pool detail, tokens, listeners)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to those frontend pages.

Endpoints:
    GET/POST   /api/v2/organizations/default/agent-pools
    GET/PATCH/DELETE /api/v2/agent-pools/{pool_id}
    POST/GET   /api/v2/agent-pools/{pool_id}/tokens
    DELETE     /api/v2/agent-pools/{pool_id}/tokens/{token_id}
    GET        /api/v2/agent-pools/{pool_id}/listeners
    POST       /api/v2/agent-pools/{pool_id}/listeners/join
    GET        /api/v2/listeners/{id}/events                   (SSE channel)
    POST       /api/v2/listeners/{id}/heartbeat
    POST       /api/v2/listeners/{id}/renew
    DELETE     /api/v2/listeners/{id}
"""

import asyncio
import json
import re
import uuid
from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from terrapod.api.dependencies import (
    DEFAULT_ORG,
    AuthenticatedUser,
    ListenerIdentity,
    get_current_user,
    get_listener_identity,
    require_admin,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import agent_pool_service
from terrapod.services.pool_rbac_service import (
    POOL_PERMISSION_HIERARCHY,
    fetch_custom_roles,
    has_pool_permission,
    resolve_pool_permission,
)

router = APIRouter(prefix="/api/v2", tags=["agent-pools"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Sanity-check email shape. Each domain segment is bounded by literal dots
# and cannot itself contain a dot, which eliminates the overlapping-quantifier
# ambiguity that would otherwise let a malicious input induce polynomial
# backtracking (CodeQL py/polynomial-redos).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")
_MAX_EMAIL_LEN = 254  # RFC 5321 §4.5.3.1.3


def _validate_owner_email(email: str | None) -> str | None:
    """Validate owner_email looks like an email address. Returns the email or raises 422."""
    if not email:
        return None
    if len(email) > _MAX_EMAIL_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"owner-email cannot exceed {_MAX_EMAIL_LEN} characters",
        )
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="owner-email must be a valid email address")
    return email


_MAX_LABELS = 50
_MAX_LABEL_KEY_LEN = 63
_MAX_LABEL_VALUE_LEN = 255


def _validate_labels(labels: dict | None) -> dict:
    """Validate labels are string key-value pairs within size limits."""
    if not labels:
        return {}
    if not isinstance(labels, dict):
        raise HTTPException(status_code=422, detail="labels must be an object")
    if len(labels) > _MAX_LABELS:
        raise HTTPException(status_code=422, detail=f"labels cannot exceed {_MAX_LABELS} entries")
    clean: dict[str, str] = {}
    for k, v in labels.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise HTTPException(status_code=422, detail="label keys and values must be strings")
        if len(k) > _MAX_LABEL_KEY_LEN:
            raise HTTPException(
                status_code=422, detail=f"label key exceeds {_MAX_LABEL_KEY_LEN} characters"
            )
        if len(v) > _MAX_LABEL_VALUE_LEN:
            raise HTTPException(
                status_code=422, detail=f"label value exceeds {_MAX_LABEL_VALUE_LEN} characters"
            )
        clean[k] = v
    return clean


def _pool_json(pool, listener_summary: dict | None = None, permission: str | None = None) -> dict:
    attrs: dict = {
        "name": pool.name,
        "description": pool.description or "",
        "labels": pool.labels or {},
        "owner-email": pool.owner_email or None,
        "created-at": _rfc3339(pool.created_at),
        "updated-at": _rfc3339(pool.updated_at),
    }
    if listener_summary is not None:
        attrs["listener-summary"] = listener_summary
    if permission is not None:
        attrs["permission"] = permission
    return {
        "id": f"apool-{pool.id}",
        "type": "agent-pools",
        "attributes": attrs,
        "relationships": {
            "organization": {
                "data": {"id": DEFAULT_ORG, "type": "organizations"},
            },
        },
    }


def _token_json(token, raw_token: str | None = None) -> dict:
    result = {
        "id": f"at-{token.id}",
        "type": "authentication-tokens",
        "attributes": {
            "description": token.description,
            "is-revoked": token.is_revoked,
            "use-count": token.use_count,
            "max-uses": token.max_uses,
            "expires-at": _rfc3339(token.expires_at) if token.expires_at else None,
            "created-at": _rfc3339(token.created_at),
            "created-by": token.created_by,
        },
    }
    if raw_token is not None:
        result["attributes"]["token"] = raw_token
    return result


def _listener_json(listener: dict) -> dict:
    """Format a Redis-backed listener dict as JSON:API.

    Accepts a dict with keys: id, name, pool_id,
    certificate_fingerprint, certificate_expires_at, created_at, last_heartbeat, etc.
    """
    return {
        "id": f"listener-{listener['id']}",
        "type": "runner-listeners",
        "attributes": {
            "name": listener.get("name", ""),
            "certificate-fingerprint": listener.get("certificate_fingerprint", ""),
            "certificate-expires-at": listener.get("certificate_expires_at", ""),
            "created-at": listener.get("created_at", ""),
            "updated-at": listener.get("last_heartbeat", listener.get("created_at", "")),
        },
        "relationships": {
            "agent-pool": {
                "data": {"id": f"apool-{listener.get('pool_id', '')}", "type": "agent-pools"},
            },
        },
    }


async def _get_pool(pool_id: str, db: AsyncSession):
    pool_uuid = uuid.UUID(pool_id.removeprefix("apool-"))
    pool = await agent_pool_service.get_pool(db, pool_uuid)
    if pool is None:
        raise HTTPException(status_code=404, detail="Agent pool not found")
    return pool


async def _require_pool_permission(
    pool, user: AuthenticatedUser, db: AsyncSession, required: str
) -> str:
    """Resolve user's pool permission and require at least `required` level.

    Returns the effective permission. Raises 404 if no access, 403 if
    insufficient.
    """
    perm = await resolve_pool_permission(
        db,
        user_email=user.email,
        user_roles=user.roles,
        pool_name=pool.name,
        pool_labels=pool.labels or {},
        owner_email=pool.owner_email or "",
    )
    if perm is None:
        raise HTTPException(status_code=404, detail="Agent pool not found")
    if not has_pool_permission(perm, required):
        raise HTTPException(status_code=403, detail=f"Requires {required} permission on pool")
    return perm


# ── Agent Pools ──────────────────────────────────────────────────────────


@router.get("/organizations/default/agent-pools")
async def list_pools(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List agent pools visible to the current user (RBAC-filtered)."""
    pools = await agent_pool_service.list_pools(db)
    # Pre-fetch custom roles once to avoid N+1 queries
    custom_roles = await fetch_custom_roles(db, user.roles)
    result = []
    for p in pools:
        perm = await resolve_pool_permission(
            db,
            user_email=user.email,
            user_roles=user.roles,
            pool_name=p.name,
            pool_labels=p.labels or {},
            owner_email=p.owner_email or "",
            preloaded_roles=custom_roles,
        )
        if perm is None:
            continue
        listeners = await agent_pool_service.list_listeners(p.id)
        summary = {
            "total": len(listeners),
            "online": sum(1 for lis in listeners if lis.get("status") == "online"),
        }
        result.append(_pool_json(p, listener_summary=summary, permission=perm))
    return JSONResponse(content={"data": result})


@router.post("/organizations/default/agent-pools", status_code=201)
async def create_pool(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create an agent pool (admin only)."""

    attrs = body.get("data", {}).get("attributes", {})
    name = attrs.get("name", "")
    if not name:
        raise HTTPException(status_code=422, detail="Pool name is required")

    pool = await agent_pool_service.create_pool(
        db,
        name=name,
        description=attrs.get("description", ""),
        labels=_validate_labels(attrs.get("labels")),
        owner_email=_validate_owner_email(attrs.get("owner-email")) or user.email,
    )
    await db.commit()
    await db.refresh(pool)

    return JSONResponse(content={"data": _pool_json(pool, permission="admin")}, status_code=201)


@router.get("/agent-pools/{pool_id}")
async def show_pool(
    pool_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show an agent pool (requires read permission)."""
    pool = await _get_pool(pool_id, db)
    perm = await _require_pool_permission(pool, user, db, "read")
    listeners = await agent_pool_service.list_listeners(pool.id)
    summary = {
        "total": len(listeners),
        "online": sum(1 for lis in listeners if lis.get("status") == "online"),
    }
    return JSONResponse(
        content={"data": _pool_json(pool, listener_summary=summary, permission=perm)}
    )


@router.patch("/agent-pools/{pool_id}")
async def update_pool(
    pool_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update an agent pool (requires admin permission on pool)."""
    pool = await _get_pool(pool_id, db)
    perm = await _require_pool_permission(pool, user, db, "admin")

    attrs = body.get("data", {}).get("attributes", {})

    # Distinguish "key absent" (don't change) from "key present with null/empty" (clear it).
    # Use _UNSET sentinel so we can detect when a key was not provided at all.
    _UNSET = object()

    owner_arg = _UNSET
    if "owner-email" in attrs:
        raw_owner = attrs["owner-email"]
        owner_arg = _validate_owner_email(raw_owner) if raw_owner else ""

    labels_arg = _UNSET
    if "labels" in attrs:
        labels_arg = _validate_labels(attrs["labels"]) if attrs["labels"] else {}

    # Self-lockout check: warn if label/owner change would reduce user's access.
    # Platform admins are immune (their access doesn't depend on labels/owner).
    if "admin" not in set(user.roles) and not attrs.get("force"):
        new_labels = labels_arg if labels_arg is not _UNSET else (pool.labels or {})
        new_owner = (owner_arg or None) if owner_arg is not _UNSET else pool.owner_email
        if new_labels != (pool.labels or {}) or new_owner != pool.owner_email:
            new_perm = await resolve_pool_permission(
                db,
                user_email=user.email,
                user_roles=user.roles,
                pool_name=attrs.get("name") or pool.name,
                pool_labels=new_labels,
                owner_email=new_owner or "",
            )
            if new_perm is None or POOL_PERMISSION_HIERARCHY.get(
                new_perm, -1
            ) < POOL_PERMISSION_HIERARCHY.get(perm, -1):
                new_level = new_perm or "none"
                return JSONResponse(
                    status_code=409,
                    content={
                        "errors": [
                            {
                                "status": "409",
                                "title": "Label/owner change would reduce your access",
                                "detail": (
                                    f"This change would reduce your access from "
                                    f"{perm} to {new_level} on this pool. "
                                    f'Re-submit with "force": true to confirm.'
                                ),
                            }
                        ]
                    },
                )

    pool = await agent_pool_service.update_pool(
        db,
        pool,
        name=attrs.get("name"),
        description=attrs.get("description"),
        labels=labels_arg if labels_arg is not _UNSET else None,
        owner_email=owner_arg if owner_arg is not _UNSET else None,
    )
    await db.commit()
    await db.refresh(pool)

    return JSONResponse(content={"data": _pool_json(pool, permission="admin")})


@router.delete("/agent-pools/{pool_id}", status_code=204)
async def delete_pool(
    pool_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an agent pool (requires admin permission on pool)."""
    pool = await _get_pool(pool_id, db)
    await _require_pool_permission(pool, user, db, "admin")
    # Clean up all listener Redis keys for this pool before DB delete
    await agent_pool_service.delete_pool_listeners(pool.id)
    await agent_pool_service.delete_pool(db, pool)
    await db.commit()


# ── Pool Tokens ──────────────────────────────────────────────────────────


@router.get("/agent-pools/{pool_id}/tokens")
async def list_pool_tokens(
    pool_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List join tokens for an agent pool (requires admin on pool)."""
    pool = await _get_pool(pool_id, db)
    await _require_pool_permission(pool, user, db, "admin")
    tokens = await agent_pool_service.list_pool_tokens(db, pool.id)
    return JSONResponse(content={"data": [_token_json(t) for t in tokens]})


@router.post("/agent-pools/{pool_id}/tokens", status_code=201)
async def create_pool_token(
    pool_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a join token for an agent pool (requires admin on pool)."""
    pool = await _get_pool(pool_id, db)
    await _require_pool_permission(pool, user, db, "admin")

    attrs = body.get("data", {}).get("attributes", {})

    token, raw_token = await agent_pool_service.create_pool_token(
        db,
        pool_id=pool.id,
        description=attrs.get("description", ""),
        created_by=user.email,
        max_uses=attrs.get("max-uses"),
    )
    await db.commit()
    await db.refresh(token)

    return JSONResponse(
        content={"data": _token_json(token, raw_token=raw_token)},
        status_code=201,
    )


@router.delete("/agent-pools/{pool_id}/tokens/{token_id}", status_code=204)
async def delete_pool_token(
    pool_id: str = Path(...),
    token_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete/revoke a join token (requires admin on pool)."""
    pool = await _get_pool(pool_id, db)
    await _require_pool_permission(pool, user, db, "admin")
    token_uuid = uuid.UUID(token_id.removeprefix("at-"))

    from sqlalchemy import select

    from terrapod.db.models import AgentPoolToken

    result = await db.execute(
        select(AgentPoolToken).where(
            AgentPoolToken.id == token_uuid,
            AgentPoolToken.pool_id == pool.id,
        )
    )
    token = result.scalar_one_or_none()
    if token is None:
        raise HTTPException(status_code=404, detail="Token not found")

    await agent_pool_service.delete_pool_token(db, token)
    await db.commit()


# ── Listener Join ────────────────────────────────────────────────────────


@router.post("/agent-pools/{pool_id}/listeners/join", status_code=201)
async def join_listener(
    pool_id: str = Path(...),
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Register a new listener via join token exchange.

    No Bearer auth — the join token in the body IS the credential.
    """
    pool = await _get_pool(pool_id, db)

    join_token = body.get("join_token", "")
    name = body.get("name", "")

    if not join_token:
        raise HTTPException(status_code=422, detail="join_token is required")
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    token = await agent_pool_service.validate_join_token(db, join_token)
    if token is None:
        raise HTTPException(status_code=401, detail="Invalid or expired join token")

    if token.pool_id != pool.id:
        raise HTTPException(status_code=403, detail="Token does not belong to this pool")

    result = await agent_pool_service.join_listener(pool, token, name, db)
    await db.commit()

    from terrapod.api.metrics import LISTENER_JOINS
    from terrapod.redis.client import POOL_EVENTS_PREFIX, publish_event

    LISTENER_JOINS.labels(pool_name=pool.name).inc()
    await publish_event(
        f"{POOL_EVENTS_PREFIX}{pool.id}",
        json.dumps({"event": "listener_joined", "listener_name": name}),
    )

    return JSONResponse(content={"data": result}, status_code=201)


@router.post("/agent-pools/join", status_code=201)
async def join_listener_by_token(
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Register a new listener using only a join token.

    The token identifies the pool — no pool ID needed in the URL.
    No Bearer auth — the join token in the body IS the credential.
    """
    join_token = body.get("join_token", "")
    name = body.get("name", "")

    if not join_token:
        raise HTTPException(status_code=422, detail="join_token is required")
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    token = await agent_pool_service.validate_join_token(db, join_token)
    if token is None:
        raise HTTPException(status_code=401, detail="Invalid or expired join token")

    pool = await agent_pool_service.get_pool(db, token.pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="Pool not found")

    result = await agent_pool_service.join_listener(pool, token, name, db)
    result["pool_id"] = str(pool.id)
    await db.commit()

    from terrapod.api.metrics import LISTENER_JOINS
    from terrapod.redis.client import POOL_EVENTS_PREFIX, publish_event

    LISTENER_JOINS.labels(pool_name=pool.name).inc()
    await publish_event(
        f"{POOL_EVENTS_PREFIX}{pool.id}",
        json.dumps({"event": "listener_joined", "listener_name": name}),
    )

    return JSONResponse(content={"data": result}, status_code=201)


# ── Listeners ────────────────────────────────────────────────────────────


@router.get("/agent-pools/{pool_id}/listeners")
async def list_pool_listeners(
    pool_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List listeners for an agent pool (requires read permission)."""
    pool = await _get_pool(pool_id, db)
    await _require_pool_permission(pool, user, db, "read")
    listeners = await agent_pool_service.list_listeners(pool.id)
    return JSONResponse(content={"data": [_listener_json(lis) for lis in listeners]})


@router.get("/agent-pools/{pool_id}/events")
async def pool_events(
    request: Request,
    pool_id: str = Path(...),
) -> EventSourceResponse:
    """Stream pool events via SSE for real-time admin updates.

    Uses short-lived DB session for auth and pool lookup, then releases
    before SSE streaming. Events: listener_heartbeat, listener_joined.
    """
    from terrapod.api.dependencies import authenticate_request
    from terrapod.db.session import get_db_session
    from terrapod.redis.client import POOL_EVENTS_PREFIX, subscribe_channel

    user = await authenticate_request(request)

    async with get_db_session() as db:
        pool = await _get_pool(pool_id, db)
        await _require_pool_permission(pool, user, db, "read")
        pool_uuid = str(pool.id)

    channel = f"{POOL_EVENTS_PREFIX}{pool_uuid}"
    pubsub = await subscribe_channel(channel)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message["type"] == "message":
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    payload = json.loads(data)
                    yield {
                        "event": payload.get("event", "update"),
                        "data": json.dumps(payload),
                    }
                else:
                    yield {"comment": "keepalive"}
                    await asyncio.sleep(1)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return EventSourceResponse(event_generator())


@router.delete("/listeners/{listener_id}", status_code=204)
async def delete_listener(
    listener_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a listener (requires admin permission on pool)."""
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    listener = await agent_pool_service.get_listener(l_uuid)
    if listener is None:
        raise HTTPException(status_code=404, detail="Listener not found")
    # Resolve pool to check admin permission
    pool_id_str = listener.get("pool_id", "")
    if not pool_id_str:
        # Orphaned listener — require platform admin to clean up
        if "admin" not in set(user.roles):
            raise HTTPException(status_code=403, detail="Admin access required")
    else:
        pool = await agent_pool_service.get_pool(db, uuid.UUID(pool_id_str))
        if pool is None:
            # Pool deleted but listener Redis key persists — require platform admin
            if "admin" not in set(user.roles):
                raise HTTPException(status_code=403, detail="Admin access required")
        else:
            await _require_pool_permission(pool, user, db, "admin")
    await agent_pool_service.delete_listener(listener["id"], listener["name"], pool_id_str)


# ── SSE Event Channel ────────────────────────────────────────────────────


@router.get("/listeners/{listener_id}/events")
async def listener_events(
    request: Request,
    listener_id: str = Path(...),
) -> EventSourceResponse:
    """SSE channel for API → listener communication.

    Uses short-lived DB session for auth and listener lookup, then releases
    before SSE streaming. The listener opens a persistent connection here.
    Events: run_available, check_job_status, stream_logs, cancel_job.
    """
    from terrapod.api.dependencies import authenticate_listener
    from terrapod.redis.client import LISTENER_EVENTS_PREFIX, subscribe_channel

    identity = await authenticate_listener(request)
    pool_id = str(identity.pool_id)

    channel = f"{LISTENER_EVENTS_PREFIX}{pool_id}"
    pubsub = await subscribe_channel(channel)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message["type"] == "message":
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    payload = json.loads(data)
                    yield {
                        "event": payload.get("event", "message"),
                        "data": data,
                    }
                else:
                    yield {"comment": "keepalive"}
                    await asyncio.sleep(1)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return EventSourceResponse(event_generator())


# ── Heartbeat & Renewal ─────────────────────────────────────────────────


@router.post("/listeners/{listener_id}/heartbeat")
async def listener_heartbeat(
    listener_id: str = Path(...),
    body: dict = Body(...),
) -> JSONResponse:
    """Listener heartbeat — refreshes TTL and updates runtime fields in Redis."""
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    listener = await agent_pool_service.get_listener(l_uuid)
    if listener is None:
        raise HTTPException(status_code=404, detail="Listener not found")

    capacity = body.get("capacity", 1)
    active_runs = body.get("active_runs", 0)

    await agent_pool_service.heartbeat_listener(
        listener_id=str(l_uuid),
        name=listener["name"],
        capacity=str(capacity),
        active_runs=str(active_runs),
    )

    from terrapod.api.metrics import LISTENER_HEARTBEATS

    LISTENER_HEARTBEATS.labels(pool_id=listener.get("pool_id", "unknown")).inc()

    # Publish heartbeat event to admin dashboard + pool SSE channels
    try:
        from terrapod.redis.client import ADMIN_EVENTS_CHANNEL, POOL_EVENTS_PREFIX, publish_event

        heartbeat_payload = json.dumps(
            {
                "event": "listener_heartbeat",
                "listener_id": str(l_uuid),
                "listener_name": listener["name"],
                "capacity": capacity,
                "active_runs": active_runs,
            }
        )
        await publish_event(ADMIN_EVENTS_CHANNEL, heartbeat_payload)
        await publish_event(f"{POOL_EVENTS_PREFIX}{listener['pool_id']}", heartbeat_payload)
    except Exception:
        pass  # Never break heartbeat for SSE

    return JSONResponse(content={"status": "ok"})


@router.post("/listeners/{listener_id}/renew")
async def renew_listener_cert(
    listener_id: str = Path(...),
    identity: ListenerIdentity = Depends(get_listener_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Renew a listener's certificate.

    Auth: must present a currently-valid client cert via
    `X-Terrapod-Client-Cert` (verified by `get_listener_identity`: CA
    signature, expiry, listener-name lookup, fingerprint match). The
    cert's listener id must also match the path id — a listener can
    only renew its own cert, not another listener's.
    """
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))

    if identity.listener_id != l_uuid:
        raise HTTPException(
            status_code=403,
            detail="Certificate does not match the listener id in the path",
        )

    listener = await agent_pool_service.get_listener(l_uuid)
    if listener is None:
        raise HTTPException(status_code=404, detail="Listener not found")

    pool_id = uuid.UUID(listener["pool_id"])
    pool = await agent_pool_service.get_pool(db, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="Agent pool not found")

    result = await agent_pool_service.renew_listener_certificate(
        listener["id"], listener["name"], pool
    )

    return JSONResponse(content={"data": result})
