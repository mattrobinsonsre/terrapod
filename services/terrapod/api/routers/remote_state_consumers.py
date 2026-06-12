"""Cross-workspace remote-state consumer allowlist CRUD (#344).

Producer-controlled allowlist authorizing which workspaces' agent runs
may read this workspace's state via ``terraform_remote_state``. Mirrors
the run-trigger router shape; the deliberate difference is that all
mutations require admin/write on the **producer** (the state owner).
Empty allowlist ⇒ not shared (secure by default).

Independent of run triggers — neither implies the other.

UX CONTRACT: Consumed by the web frontend on the workspace detail page
(Remote State Sharing panel). Changes to response shapes, attribute
names, or status codes here MUST be matched by corresponding updates
to that frontend page.

Endpoints:
    POST   /api/terrapod/v1/workspaces/{id}/remote-state-consumers      (add one)
    GET    /api/terrapod/v1/workspaces/{id}/remote-state-consumers       (list inbound/outbound)
    PUT    /api/terrapod/v1/workspaces/{id}/remote-state-consumers       (replace whole set)
    GET    /api/terrapod/v1/remote-state-consumers/{id}                   (show)
    DELETE /api/terrapod/v1/remote-state-consumers/{id}                   (delete)
"""

import uuid
from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import Workspace, WorkspaceRemoteStateConsumer
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.workspace_rbac_service import (
    has_permission,
    resolve_workspace_permission_for,
)

router = APIRouter(tags=["remote-state-consumers"])
logger = get_logger(__name__)

# Generous defensive cap to prevent runaway allowlists. A shared-module
# workspace consumed by many environments is normal; a list larger than
# this almost certainly indicates a label-driven case better served by
# a future selector mechanism (out of scope here).
MAX_CONSUMERS_PER_PRODUCER = 200


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _consumer_json(row: WorkspaceRemoteStateConsumer) -> dict:
    """Serialize a WorkspaceRemoteStateConsumer to JSON:API format."""
    edge_id = f"rsc-{row.id}"
    producer_name = row.producer_workspace.name if row.producer_workspace else ""
    consumer_name = row.consumer_workspace.name if row.consumer_workspace else ""

    return {
        "id": edge_id,
        "type": "remote-state-consumers",
        "attributes": {
            "producer-workspace-name": producer_name,
            "consumer-workspace-name": consumer_name,
            "created-at": _rfc3339(row.created_at),
            "created-by": row.created_by or "",
        },
        "relationships": {
            "producer": {
                "data": {"id": f"ws-{row.producer_workspace_id}", "type": "workspaces"},
            },
            "consumer": {
                "data": {"id": f"ws-{row.consumer_workspace_id}", "type": "workspaces"},
            },
        },
        "links": {
            "self": f"/api/terrapod/v1/remote-state-consumers/{edge_id}",
        },
    }


async def _get_workspace(workspace_id: str, db: AsyncSession) -> Workspace:
    ws_uuid = workspace_id.removeprefix("ws-")
    try:
        ws_uuid_obj = uuid.UUID(ws_uuid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Workspace not found") from exc
    result = await db.execute(select(Workspace).where(Workspace.id == ws_uuid_obj))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


async def _require_ws_permission(
    ws: Workspace, required: str, user: AuthenticatedUser, db: AsyncSession
) -> None:
    perm = await resolve_workspace_permission_for(db, user, ws)
    if not has_permission(perm, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {required} permission on workspace",
        )


def _extract_consumer_id(body: dict) -> str:
    """Pull the consumer workspace id out of a JSON:API request body."""
    relationships = body.get("data", {}).get("relationships", {})
    consumer_data = relationships.get("consumer", {}).get("data", {})
    consumer_id = consumer_data.get("id", "")
    if not consumer_id:
        raise HTTPException(
            status_code=422,
            detail="consumer relationship is required (relationships.consumer.data.id)",
        )
    return consumer_id


@router.post("/workspaces/{workspace_id}/remote-state-consumers", status_code=201)
async def create_remote_state_consumer(
    workspace_id: str = Path(..., description="Producer workspace id"),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Authorize a consumer workspace to read this (producer) workspace's
    state. Requires **admin** on the producer (the state owner). Producer
    control of the secret-bearing state is the security invariant.
    """
    producer = await _get_workspace(workspace_id, db)
    await _require_ws_permission(producer, "admin", user, db)

    consumer = await _get_workspace(_extract_consumer_id(body), db)

    if producer.id == consumer.id:
        raise HTTPException(
            status_code=422,
            detail="A workspace already reads its own state; self-reference is not a grant",
        )

    existing = await db.execute(
        select(WorkspaceRemoteStateConsumer).where(
            WorkspaceRemoteStateConsumer.producer_workspace_id == producer.id,
            WorkspaceRemoteStateConsumer.consumer_workspace_id == consumer.id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Consumer is already authorized")

    count = (
        await db.execute(
            select(func.count())
            .select_from(WorkspaceRemoteStateConsumer)
            .where(WorkspaceRemoteStateConsumer.producer_workspace_id == producer.id)
        )
    ).scalar_one()
    if count >= MAX_CONSUMERS_PER_PRODUCER:
        raise HTTPException(
            status_code=422,
            detail=f"Maximum of {MAX_CONSUMERS_PER_PRODUCER} consumers per producer",
        )

    row = WorkspaceRemoteStateConsumer(
        producer_workspace_id=producer.id,
        consumer_workspace_id=consumer.id,
        created_by=user.email or "",
    )
    db.add(row)
    await db.flush()
    await db.refresh(row, attribute_names=["producer_workspace", "consumer_workspace"])
    await db.commit()

    # Live-update both Sharing tabs: the producer gets a new outbound consumer,
    # the consumer a new inbound producer it may now read from.
    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(producer.id), "remote_state_consumer_change")
    await publish_workspace_event(str(consumer.id), "remote_state_consumer_change")

    logger.info(
        "Remote-state consumer authorized",
        edge_id=str(row.id),
        producer=producer.name,
        consumer=consumer.name,
        by=user.email,
    )

    return JSONResponse(content={"data": _consumer_json(row)}, status_code=201)


@router.get("/workspaces/{workspace_id}/remote-state-consumers")
async def list_remote_state_consumers(
    workspace_id: str = Path(...),
    filter_type: str | None = Query(None, alias="filter[remote-state-consumer][type]"),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List remote-state consumer edges for a workspace.

    ``outbound`` (default): workspaces I share my state with (I'm the
    producer). ``inbound``: workspaces whose state I'm authorized to
    read (I'm the consumer). Requires read on the workspace.
    """
    ws = await _get_workspace(workspace_id, db)
    await _require_ws_permission(ws, "read", user, db)

    if filter_type is None:
        filter_type = "outbound"
    if filter_type not in ("inbound", "outbound"):
        raise HTTPException(
            status_code=422,
            detail=("filter[remote-state-consumer][type] must be 'inbound' or 'outbound'"),
        )

    if filter_type == "outbound":
        where = WorkspaceRemoteStateConsumer.producer_workspace_id == ws.id
    else:
        where = WorkspaceRemoteStateConsumer.consumer_workspace_id == ws.id

    query = (
        select(WorkspaceRemoteStateConsumer)
        .options(
            selectinload(WorkspaceRemoteStateConsumer.producer_workspace),
            selectinload(WorkspaceRemoteStateConsumer.consumer_workspace),
        )
        .where(where)
        .order_by(WorkspaceRemoteStateConsumer.created_at.asc())
    )
    rows = list((await db.execute(query)).scalars().all())

    return JSONResponse(content={"data": [_consumer_json(r) for r in rows]})


@router.put("/workspaces/{workspace_id}/remote-state-consumers")
async def replace_remote_state_consumers(
    workspace_id: str = Path(..., description="Producer workspace id"),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Declaratively replace the producer's full consumer set in one
    atomic transaction. Provided to support the provider's set-valued
    ``remote_state_consumers`` attribute (idempotent). Requires admin
    on the producer.

    Body shape: ``{"data": [{"type": "workspaces", "id": "ws-..."}, ...]}``.
    """
    producer = await _get_workspace(workspace_id, db)
    await _require_ws_permission(producer, "admin", user, db)

    items = body.get("data", [])
    if not isinstance(items, list):
        raise HTTPException(
            status_code=422,
            detail="data must be a list of workspace references",
        )

    desired: set[uuid.UUID] = set()
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="invalid consumer reference")
        raw_id = item.get("id", "")
        consumer = await _get_workspace(raw_id, db)
        if consumer.id == producer.id:
            raise HTTPException(
                status_code=422,
                detail="A workspace already reads its own state; self-reference is not a grant",
            )
        desired.add(consumer.id)

    if len(desired) > MAX_CONSUMERS_PER_PRODUCER:
        raise HTTPException(
            status_code=422,
            detail=f"Maximum of {MAX_CONSUMERS_PER_PRODUCER} consumers per producer",
        )

    existing_q = await db.execute(
        select(WorkspaceRemoteStateConsumer).where(
            WorkspaceRemoteStateConsumer.producer_workspace_id == producer.id
        )
    )
    existing_rows = list(existing_q.scalars().all())
    existing_ids = {r.consumer_workspace_id for r in existing_rows}

    # Remove rows no longer in the desired set.
    for r in existing_rows:
        if r.consumer_workspace_id not in desired:
            await db.delete(r)

    # Add rows newly in the desired set.
    for cid in desired - existing_ids:
        db.add(
            WorkspaceRemoteStateConsumer(
                producer_workspace_id=producer.id,
                consumer_workspace_id=cid,
                created_by=user.email or "",
            )
        )

    await db.flush()

    refreshed_q = await db.execute(
        select(WorkspaceRemoteStateConsumer)
        .options(
            selectinload(WorkspaceRemoteStateConsumer.producer_workspace),
            selectinload(WorkspaceRemoteStateConsumer.consumer_workspace),
        )
        .where(WorkspaceRemoteStateConsumer.producer_workspace_id == producer.id)
        .order_by(WorkspaceRemoteStateConsumer.created_at.asc())
    )
    rows = list(refreshed_q.scalars().all())

    await db.commit()

    logger.info(
        "Remote-state consumer set replaced",
        producer=producer.name,
        count=len(rows),
        by=user.email,
    )

    return JSONResponse(content={"data": [_consumer_json(r) for r in rows]})


@router.get("/remote-state-consumers/{edge_id}")
async def show_remote_state_consumer(
    edge_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a single consumer edge. Requires read on the producer."""
    try:
        edge_uuid = uuid.UUID(edge_id.removeprefix("rsc-"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Remote-state consumer not found") from exc

    result = await db.execute(
        select(WorkspaceRemoteStateConsumer)
        .options(
            selectinload(WorkspaceRemoteStateConsumer.producer_workspace),
            selectinload(WorkspaceRemoteStateConsumer.consumer_workspace),
        )
        .where(WorkspaceRemoteStateConsumer.id == edge_uuid)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Remote-state consumer not found")

    await _require_ws_permission(row.producer_workspace, "read", user, db)

    return JSONResponse(content={"data": _consumer_json(row)})


@router.delete("/remote-state-consumers/{edge_id}", status_code=204)
async def delete_remote_state_consumer(
    edge_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a consumer grant. Requires admin on the **producer** —
    the state owner controls who may read; a consumer cannot revoke
    nor self-grant.
    """
    try:
        edge_uuid = uuid.UUID(edge_id.removeprefix("rsc-"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Remote-state consumer not found") from exc

    result = await db.execute(
        select(WorkspaceRemoteStateConsumer)
        .options(selectinload(WorkspaceRemoteStateConsumer.producer_workspace))
        .where(WorkspaceRemoteStateConsumer.id == edge_uuid)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Remote-state consumer not found")

    await _require_ws_permission(row.producer_workspace, "admin", user, db)

    producer_id = str(row.producer_workspace_id)
    consumer_id = str(row.consumer_workspace_id)
    revoked_edge_id = str(row.id)

    await db.delete(row)
    await db.commit()

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(producer_id, "remote_state_consumer_change")
    await publish_workspace_event(consumer_id, "remote_state_consumer_change")

    logger.info(
        "Remote-state consumer revoked",
        edge_id=revoked_edge_id,
        by=user.email,
    )
