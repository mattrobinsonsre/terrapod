"""Agent pool, join token, listener management, and heartbeat endpoints.

Endpoints:
    GET/POST   /api/v2/organizations/{org}/agent-pools
    GET/PATCH/DELETE /api/v2/agent-pools/{pool_id}
    POST/GET   /api/v2/agent-pools/{pool_id}/tokens
    DELETE     /api/v2/agent-pools/{pool_id}/tokens/{token_id}
    GET        /api/v2/agent-pools/{pool_id}/listeners
    POST       /api/v2/agent-pools/{pool_id}/listeners/join
    POST       /api/v2/listeners/{id}/heartbeat
    POST       /api/v2/listeners/{id}/renew
    DELETE     /api/v2/listeners/{id}
"""

import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_admin
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import agent_pool_service

router = APIRouter(prefix="/api/v2", tags=["agent-pools"])
logger = get_logger(__name__)

DEFAULT_ORG = "default"


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    from datetime import timezone
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pool_json(pool) -> dict:
    return {
        "id": f"apool-{pool.id}",
        "type": "agent-pools",
        "attributes": {
            "name": pool.name,
            "description": pool.description or "",
            "service-account-name": pool.service_account_name,
            "created-at": _rfc3339(pool.created_at),
            "updated-at": _rfc3339(pool.updated_at),
        },
        "relationships": {
            "organization": {
                "data": {"id": pool.org_name or DEFAULT_ORG, "type": "organizations"},
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


def _validate_org(org: str) -> None:
    if org != DEFAULT_ORG:
        raise HTTPException(status_code=404, detail="Organization not found")


async def _get_pool(pool_id: str, db: AsyncSession):
    pool_uuid = uuid.UUID(pool_id.removeprefix("apool-"))
    pool = await agent_pool_service.get_pool(db, pool_uuid)
    if pool is None:
        raise HTTPException(status_code=404, detail="Agent pool not found")
    return pool


# ── Agent Pools ──────────────────────────────────────────────────────────


@router.get("/organizations/{org}/agent-pools")
async def list_pools(
    org: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all agent pools."""
    _validate_org(org)
    pools = await agent_pool_service.list_pools(db, org)
    return JSONResponse(content={"data": [_pool_json(p) for p in pools]})


@router.post("/organizations/{org}/agent-pools", status_code=201)
async def create_pool(
    org: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create an agent pool (admin only)."""
    _validate_org(org)

    attrs = body.get("data", {}).get("attributes", {})
    name = attrs.get("name", "")
    if not name:
        raise HTTPException(status_code=422, detail="Pool name is required")

    pool = await agent_pool_service.create_pool(
        db,
        name=name,
        description=attrs.get("description", ""),
        org_name=org,
        service_account_name=attrs.get("service-account-name", ""),
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
        service_account_name=attrs.get("service-account-name"),
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

    result = await agent_pool_service.join_listener(
        db, pool, token, name, runner_definitions
    )
    await db.commit()

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
    return JSONResponse(content={"data": [_listener_json(l) for l in listeners]})


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

    from terrapod.redis.client import get_redis

    redis = get_redis()
    prefix = f"tp:listener:{listener.id}"
    ttl = 180  # seconds

    capacity = body.get("capacity", 1)
    active_runs = body.get("active_runs", 0)
    runner_defs = body.get("runner_definitions", [])

    import json

    await redis.setex(f"{prefix}:status", ttl, "online")
    await redis.setex(f"{prefix}:heartbeat", ttl, str(int(__import__("time").time())))
    await redis.setex(f"{prefix}:capacity", ttl, str(capacity))
    await redis.setex(f"{prefix}:active_runs", ttl, str(active_runs))
    await redis.setex(f"{prefix}:runner_defs", ttl, json.dumps(runner_defs))

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
