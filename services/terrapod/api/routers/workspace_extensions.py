"""Terrapod-specific workspace extension endpoints.

These endpoints are NOT part of the TFE V2 API specification. They provide
Terrapod-specific functionality consumed by the web UI.

Endpoints:
    GET  /api/terrapod/v1/workspace-events — SSE stream for workspace list updates
    GET  /api/terrapod/v1/workspaces/{workspace_id}/vcs-refs — list VCS branches/tags
    POST /api/terrapod/v1/workspaces/{workspace_id}/actions/dismiss-drift — clear drift status
"""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.workspace_rbac_service import (
    resolve_workspace_capabilities_for,
)

router = APIRouter(tags=["workspace-extensions"])
logger = get_logger(__name__)


# ── SSE (Server-Sent Events) ─────────────────────────────────────────────
# This MUST come before parameterized /workspaces/{workspace_id} routes
# so FastAPI doesn't match "workspace-events" as a workspace_id parameter.


@router.get("/workspace-events")
async def workspace_list_events(
    request: Request,
) -> EventSourceResponse:
    """Stream workspace list events via SSE for real-time updates.

    Any authenticated user can subscribe. Uses short-lived DB session
    for auth, then releases before SSE streaming.
    """
    from terrapod.api.dependencies import authenticate_request
    from terrapod.redis.client import WORKSPACE_LIST_EVENTS_CHANNEL, subscribe_channel

    await authenticate_request(request)

    pubsub = await subscribe_channel(WORKSPACE_LIST_EVENTS_CHANNEL)

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
            await pubsub.unsubscribe(WORKSPACE_LIST_EVENTS_CHANNEL)
            await pubsub.aclose()

    return EventSourceResponse(event_generator())


@router.get("/workspaces/{workspace_id}/vcs-refs")
async def list_vcs_refs(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List branches, tags, and default branch for a VCS-connected workspace.

    Requires read permission on the workspace.
    """
    from terrapod.api.routers.tfe_v2 import _get_workspace_by_id
    from terrapod.db.models import VCSConnection
    from terrapod.services.vcs_poller import (
        _list_branches,
        _list_tags,
        _parse_repo_url,
        _resolve_branch,
    )

    ws = await _get_workspace_by_id(workspace_id, db)
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.WORKSPACE_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires workspace:read capability on workspace",
        )

    if not ws.vcs_connection_id or not ws.vcs_repo_url:
        raise HTTPException(status_code=422, detail="Workspace is not VCS-connected")

    conn = await db.get(VCSConnection, ws.vcs_connection_id)
    if not conn or conn.status != "active":
        raise HTTPException(status_code=422, detail="VCS connection is not active")

    parsed = _parse_repo_url(conn, ws.vcs_repo_url)
    if not parsed:
        raise HTTPException(status_code=422, detail="Cannot parse VCS repo URL")
    owner, repo = parsed

    branches = await _list_branches(conn, owner, repo)
    tags = await _list_tags(conn, owner, repo)
    default_branch = await _resolve_branch(conn, ws, owner, repo) or ""

    return JSONResponse(
        content={
            "branches": branches,
            "tags": tags,
            "default-branch": default_branch,
        }
    )


@router.post("/workspaces/{workspace_id}/actions/dismiss-drift")
async def dismiss_drift(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Clear the workspace's transient drift_status without disabling drift detection.

    Sets `drift_status = ""` and `drift_last_checked_at = null`. Leaves
    `drift_detection_enabled` unchanged — scheduled checks continue to run.
    The next scheduled check will repopulate the state from the current
    infrastructure reality.

    Idempotent: dismissing when no drift is currently reported is a no-op.

    Requires `plan` permission on the workspace (same level as lock/unlock —
    a transient state reset, not a configuration mutation).
    """
    from terrapod.api.routers.tfe_v2 import _get_workspace_by_id

    ws = await _get_workspace_by_id(workspace_id, db)
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.DRIFT_DISMISS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires drift:dismiss capability on workspace",
        )

    ws.drift_status = ""
    ws.drift_last_checked_at = None
    await db.commit()

    logger.info(
        "Drift status dismissed",
        workspace=ws.name,
        user=user.email,
    )

    return JSONResponse(
        content={
            "data": {
                "id": f"ws-{ws.id}",
                "type": "workspaces",
                "attributes": {
                    "drift-status": ws.drift_status,
                    "drift-last-checked-at": None,
                    "drift-detection-enabled": ws.drift_detection_enabled,
                },
            }
        }
    )


@router.get("/estate-graph")
async def show_estate_graph(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Whole-estate topology graph for the Estate page (#763).

    Terrapod-native. Derives ``{nodes, edges, meta}`` server-side from the
    cross-workspace structure (remote-state consumers, run-triggers, module
    links), RBAC-filtered to the workspaces the caller can read. See
    ``estate_graph_service`` — the grouping axis is chosen client-side (the
    platform enforces no labelling convention), so the payload is deliberately
    label-agnostic.
    """
    from terrapod.services import estate_graph_service

    graph = await estate_graph_service.derive_estate_graph(db, user)
    return JSONResponse(
        content={"data": {"id": "estate-graph", "type": "estate-graphs", "attributes": graph}}
    )


@router.get("/workspaces/{workspace_id}/state-graph")
async def show_state_graph(
    workspace_id: str = Path(...),
    state_version: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Resource dependency graph for a workspace's state version (#765).

    Terrapod-native. Derives ``{nodes, edges, meta}`` from the Terraform state
    blob — one node per resource address, ``depends-on`` edges from each
    instance's ``dependencies``. Defaults to the current (highest-serial) state
    version; ``?state_version=sv-...`` renders an older one. ``meta.versions``
    carries the picker list. Gated on ``state:read`` (the graph is derived from
    the secret-bearing state blob). See ``state_graph_service``.
    """
    from terrapod.services import state_graph_service

    graph = await state_graph_service.derive_state_graph(db, user, workspace_id, state_version)
    return JSONResponse(
        content={"data": {"id": "state-graph", "type": "state-graphs", "attributes": graph}}
    )
