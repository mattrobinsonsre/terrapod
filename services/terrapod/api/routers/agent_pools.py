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
import uuid
from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from terrapod.api.dependencies import (
    DEFAULT_ORG,
    AuthenticatedUser,
    get_current_user,
    require_admin,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import agent_pool_service

router = APIRouter(prefix="/api/v2", tags=["agent-pools"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pool_json(pool) -> dict:
    return {
        "id": f"apool-{pool.id}",
        "type": "agent-pools",
        "attributes": {
            "name": pool.name,
            "description": pool.description or "",
            "created-at": _rfc3339(pool.created_at),
            "updated-at": _rfc3339(pool.updated_at),
        },
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


def _listener_json(listener) -> dict:
    return {
        "id": f"listener-{listener.id}",
        "type": "runner-listeners",
        "attributes": {
            "name": listener.name,
            "runner-definitions": listener.runner_definitions,
            "certificate-fingerprint": listener.certificate_fingerprint or "",
            "certificate-expires-at": _rfc3339(listener.certificate_expires_at),
            "created-at": _rfc3339(listener.created_at),
            "updated-at": _rfc3339(listener.updated_at),
        },
        "relationships": {
            "agent-pool": {
                "data": {"id": f"apool-{listener.pool_id}", "type": "agent-pools"},
            },
        },
    }


async def _get_pool(pool_id: str, db: AsyncSession):
    pool_uuid = uuid.UUID(pool_id.removeprefix("apool-"))
    pool = await agent_pool_service.get_pool(db, pool_uuid)
    if pool is None:
        raise HTTPException(status_code=404, detail="Agent pool not found")
    return pool


# ── Agent Pools ──────────────────────────────────────────────────────────


@router.get("/organizations/default/agent-pools")
async def list_pools(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all agent pools."""
    pools = await agent_pool_service.list_pools(db)
    return JSONResponse(content={"data": [_pool_json(p) for p in pools]})


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
    )
    await db.commit()
    await db.refresh(pool)

    return JSONResponse(content={"data": _pool_json(pool)}, status_code=201)


@router.get("/agent-pools/{pool_id}")
async def show_pool(
    pool_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show an agent pool."""
    pool = await _get_pool(pool_id, db)
    return JSONResponse(content={"data": _pool_json(pool)})


@router.patch("/agent-pools/{pool_id}")
async def update_pool(
    pool_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update an agent pool (admin only)."""
    pool = await _get_pool(pool_id, db)

    attrs = body.get("data", {}).get("attributes", {})
    pool = await agent_pool_service.update_pool(
        db,
        pool,
        name=attrs.get("name"),
        description=attrs.get("description"),
    )
    await db.commit()
    await db.refresh(pool)

    return JSONResponse(content={"data": _pool_json(pool)})


@router.delete("/agent-pools/{pool_id}", status_code=204)
async def delete_pool(
    pool_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an agent pool (admin only)."""
    pool = await _get_pool(pool_id, db)
    await agent_pool_service.delete_pool(db, pool)
    await db.commit()


# ── Pool Tokens ──────────────────────────────────────────────────────────


@router.get("/agent-pools/{pool_id}/tokens")
async def list_pool_tokens(
    pool_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List join tokens for an agent pool."""
    pool = await _get_pool(pool_id, db)
    tokens = await agent_pool_service.list_pool_tokens(db, pool.id)
    return JSONResponse(content={"data": [_token_json(t) for t in tokens]})


@router.post("/agent-pools/{pool_id}/tokens", status_code=201)
async def create_pool_token(
    pool_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a join token for an agent pool."""
    pool = await _get_pool(pool_id, db)

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
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete/revoke a join token."""
    pool = await _get_pool(pool_id, db)
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
    runner_definitions = body.get("runner_definitions", ["standard"])

    if not join_token:
        raise HTTPException(status_code=422, detail="join_token is required")
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    token = await agent_pool_service.validate_join_token(db, join_token)
    if token is None:
        raise HTTPException(status_code=401, detail="Invalid or expired join token")

    if token.pool_id != pool.id:
        raise HTTPException(status_code=403, detail="Token does not belong to this pool")

    result = await agent_pool_service.join_listener(db, pool, token, name, runner_definitions)
    await db.commit()

    from terrapod.redis.client import POOL_EVENTS_PREFIX, publish_event

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
    runner_definitions = body.get("runner_definitions", ["standard"])

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

    result = await agent_pool_service.join_listener(db, pool, token, name, runner_definitions)
    result["pool_id"] = str(pool.id)
    await db.commit()

    from terrapod.redis.client import POOL_EVENTS_PREFIX, publish_event

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
    """List listeners for an agent pool."""
    pool = await _get_pool(pool_id, db)
    listeners = await agent_pool_service.list_listeners(db, pool.id)
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
    if "admin" not in user.roles:
        raise HTTPException(status_code=403, detail="Admin access required")

    async with get_db_session() as db:
        pool = await _get_pool(pool_id, db)
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
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a listener (admin only)."""
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    listener = await agent_pool_service.get_listener(db, l_uuid)
    if listener is None:
        raise HTTPException(status_code=404, detail="Listener not found")
    await agent_pool_service.delete_listener(db, listener)
    await db.commit()


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
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Listener heartbeat — updates Redis state.

    Auth: Currently accepts any request with a valid listener ID.
    Certificate-based auth will be added via X-Terrapod-Client-Cert header.
    """
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    listener = await agent_pool_service.get_listener(db, l_uuid)
    if listener is None:
        raise HTTPException(status_code=404, detail="Listener not found")

    from terrapod.redis.client import get_redis_client

    redis = get_redis_client()
    prefix = f"tp:listener:{listener.id}"
    ttl = 180  # seconds

    capacity = body.get("capacity", 1)
    active_runs = body.get("active_runs", 0)
    runner_defs = body.get("runner_definitions", [])

    await redis.setex(f"{prefix}:status", ttl, "online")
    await redis.setex(f"{prefix}:heartbeat", ttl, str(int(__import__("time").time())))
    await redis.setex(f"{prefix}:capacity", ttl, str(capacity))
    await redis.setex(f"{prefix}:active_runs", ttl, str(active_runs))
    await redis.setex(f"{prefix}:runner_defs", ttl, json.dumps(runner_defs))

    # Publish heartbeat event to admin dashboard + pool SSE channels
    try:
        from terrapod.redis.client import ADMIN_EVENTS_CHANNEL, POOL_EVENTS_PREFIX, publish_event

        heartbeat_payload = json.dumps(
            {
                "event": "listener_heartbeat",
                "listener_id": str(listener.id),
                "listener_name": listener.name,
                "capacity": capacity,
                "active_runs": active_runs,
            }
        )
        await publish_event(ADMIN_EVENTS_CHANNEL, heartbeat_payload)
        await publish_event(f"{POOL_EVENTS_PREFIX}{listener.pool_id}", heartbeat_payload)
    except Exception:
        pass  # Never break heartbeat for SSE

    return JSONResponse(content={"status": "ok"})


@router.post("/listeners/{listener_id}/renew")
async def renew_listener_cert(
    listener_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Renew a listener's certificate.

    Auth: Certificate-based (X-Terrapod-Client-Cert). For now accepts
    any request with a valid listener ID.
    """
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    listener = await agent_pool_service.get_listener(db, l_uuid)
    if listener is None:
        raise HTTPException(status_code=404, detail="Listener not found")

    pool = await agent_pool_service.get_pool(db, listener.pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="Agent pool not found")

    result = await agent_pool_service.renew_listener_certificate(db, listener, pool)
    await db.commit()

    return JSONResponse(content={"data": result})
