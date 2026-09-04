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

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.errors import vcs_unavailable
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

    # This backs the ref picker in the run dialog, so a provider outage is felt
    # here before the operator has even pressed anything. Naming the provider
    # beats a 500 that reads as Terrapod being broken.
    try:
        branches = await _list_branches(conn, owner, repo)
        tags = await _list_tags(conn, owner, repo)
        default_branch = await _resolve_branch(conn, ws, owner, repo) or ""
    except httpx.HTTPError as e:
        raise vcs_unavailable(conn, f"{owner}/{repo}", "", e) from e

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


# ── AI architecture critic (#1036 Part 2 / #963) ─────────────────────────────
# State-based, whole-system critique. Read is gated on `state:read` (same trust
# as the state-graph — derived from the secret-bearing state). The critique is
# generated server-side by `architecture_critic_service` and rides the existing
# per-workspace run-events SSE channel (architecture_critique_{pending,ready,…}).


def _rfc3339(value) -> str:
    return value.isoformat().replace("+00:00", "Z") if value else ""


def _critique_json(c, *, translated_fields: dict | None = None, translated: bool = False) -> dict:
    tf = translated_fields or {}
    return {
        "data": {
            "id": f"architecture-critique-{c.id}",
            "type": "architecture-critiques",
            "attributes": {
                "status": c.status,
                "state-serial": c.state_serial,
                "risk-level": c.risk_level,
                "architecture": tf.get("architecture", c.architecture),
                "findings": tf.get("findings", c.findings),
                "deferred": tf.get("deferred", c.deferred),
                "translated": translated,
                "model": c.model,
                "input-tokens": c.input_tokens,
                "output-tokens": c.output_tokens,
                "error-message": c.error_message,
                "created-at": _rfc3339(c.created_at),
                "updated-at": _rfc3339(c.updated_at),
            },
        }
    }


async def _resolve_workspace_state_read(
    db: AsyncSession, user: AuthenticatedUser, workspace_id: str
):
    """Resolve the workspace and enforce ``state:read``; return the Workspace."""
    from terrapod.api.routers.tfe_v2 import _get_workspace_by_id

    ws = await _get_workspace_by_id(workspace_id, db)
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.STATE_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires state:read permission on workspace",
        )
    return ws


@router.get("/workspaces/{workspace_id}/architecture-critique")
async def get_architecture_critique(
    request: Request,
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """The AI architecture critique for the workspace's CURRENT state version.

    Terrapod-native, gated on ``state:read``. 404 when the feature is disabled,
    the workspace has no state, or no critique has been generated for the current
    state yet (the UI treats 404 as "absent" and offers to generate).

    When ``locale`` is supplied, a ready critique's prose (architecture summary,
    findings, deferred) is translated on view into that language and cached in
    Redis (#767/#1036) — the code-shaped fields (severity/category/addresses)
    stay verbatim. Falls back to the canonical text if translation doesn't apply.
    """
    from sqlalchemy import select

    from terrapod.config import settings
    from terrapod.db.models import ArchitectureCritique, StateVersion

    if not settings.ai_architecture.enabled:
        raise HTTPException(status_code=404, detail="architecture critic not enabled")

    ws = await _resolve_workspace_state_read(db, user, workspace_id)
    sv = (
        await db.execute(
            select(StateVersion)
            .where(StateVersion.workspace_id == ws.id)
            .order_by(StateVersion.serial.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if sv is None:
        raise HTTPException(status_code=404, detail="workspace has no state")
    critique = (
        await db.execute(
            select(ArchitectureCritique).where(ArchitectureCritique.state_version_id == sv.id)
        )
    ).scalar_one_or_none()
    if critique is None:
        raise HTTPException(status_code=404, detail="no critique for current state")

    # Read the optional locale off the raw request (not a declared Query param)
    # so the released route signature is unchanged — the contract-safe pattern
    # (AGENTS.md pagination convention).
    locale = request.query_params.get("locale")
    if locale and critique.status == "ready":
        from terrapod.services import summary_translation

        tr = await summary_translation.translate_architecture_critique(
            critique_id=str(critique.id),
            architecture=critique.architecture or {},
            findings=critique.findings or [],
            deferred=critique.deferred or [],
            reader_locale=locale,
        )
        if tr is not None:
            return JSONResponse(
                content=_critique_json(critique, translated_fields=tr, translated=True)
            )
    return JSONResponse(content=_critique_json(critique))


@router.post("/workspaces/{workspace_id}/architecture-critique/regenerate")
async def regenerate_architecture_critique(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Queue a fresh architecture critique for the workspace's current state.

    Gated on ``state:read`` (the critique reasons over the secret-bearing state).
    Mutates no infrastructure — it enqueues the async critic. Returns 202; the UI
    picks up ``architecture_critique_{pending,ready}`` via the workspace SSE.
    """
    from terrapod.config import settings

    if not settings.ai_architecture.enabled:
        raise HTTPException(status_code=404, detail="architecture critic not enabled")

    await _resolve_workspace_state_read(db, user, workspace_id)

    from terrapod.services.scheduler import enqueue_trigger

    await enqueue_trigger(
        "architecture_critique",
        {"workspace_id": workspace_id, "force": True},
        dedup_key=f"arch-regen:{workspace_id}",
    )
    return JSONResponse(
        status_code=202,
        content={
            "data": {
                "id": f"architecture-critique-regenerate-{workspace_id}",
                "type": "architecture-critiques",
                "attributes": {"status": "pending"},
            }
        },
    )


def _critique_message_json(m, *, content: str | None = None) -> dict:
    return {
        "id": f"architecture-critique-message-{m.id}",
        "type": "architecture-critique-messages",
        "attributes": {
            "role": m.role,
            "content": content if content is not None else m.content,
            "model": m.model,
            "input-tokens": m.input_tokens,
            "output-tokens": m.output_tokens,
            "error-message": m.error_message,
            "created-at": _rfc3339(m.created_at),
        },
    }


async def _translated_message_json(m, reader_locale: str | None) -> dict:
    """Serialize a chat message, translating an assistant reply's prose on view
    (#767) when a reader locale is set. User rows + errored rows pass through."""
    if reader_locale and m.role == "assistant" and m.content and not m.error_message:
        from terrapod.services import summary_translation

        tr = await summary_translation.translate_message(
            message_id=str(m.id), content=m.content, reader_locale=reader_locale
        )
        if tr is not None:
            return _critique_message_json(m, content=tr)
    return _critique_message_json(m)


@router.get("/workspaces/{workspace_id}/architecture-critique/messages")
async def list_architecture_critique_messages(
    request: Request,
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """The follow-up chat thread for the workspace's current architecture critique.

    Gated on ``state:read`` (same as the critique itself). 404 when the feature
    is disabled or there's no critique for the current state. Empty list is fine.
    Assistant replies are translated on view into ``locale`` when supplied.
    """
    from terrapod.config import settings
    from terrapod.services import architecture_critic_service as critic

    if not settings.ai_architecture.enabled:
        raise HTTPException(status_code=404, detail="architecture critic not enabled")
    ws = await _resolve_workspace_state_read(db, user, workspace_id)
    critique = await critic.current_critique_for_workspace(db, ws.id)
    if critique is None:
        raise HTTPException(status_code=404, detail="no critique for current state")
    msgs = await critic.list_critique_messages(db, critique.id)
    locale = request.query_params.get("locale")  # off Request → contract-safe
    data = [await _translated_message_json(m, locale) for m in msgs]
    return JSONResponse(content={"data": data})


@router.post("/workspaces/{workspace_id}/architecture-critique/messages")
async def post_architecture_critique_message(
    request: Request,
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Post one operator follow-up against the current critique; returns the
    assistant reply. Gated on ``state:read``. 503/409/429 map to feature-off /
    cap-reached / budget-exhausted (mirroring the plan-summary chat)."""
    from terrapod.config import settings
    from terrapod.services import architecture_critic_service as critic
    from terrapod.services.summariser import (
        FollowupBudgetExhausted,
        FollowupCapReached,
        FollowupDisabled,
        FollowupError,
    )

    if not settings.ai_architecture.enabled:
        raise HTTPException(status_code=404, detail="architecture critic not enabled")
    ws = await _resolve_workspace_state_read(db, user, workspace_id)

    try:
        body = await request.json()
        attrs = body["data"]["attributes"]
        content = str(attrs["content"] or "").strip()
        locale = str(attrs["locale"]) if attrs.get("locale") else None
    except KeyError, TypeError, ValueError:
        raise HTTPException(status_code=400, detail="malformed body") from None
    if not content:
        raise HTTPException(status_code=422, detail="content is required")

    # Normalise the question into the system language so the stored thread stays
    # monolingual/authoritative, then translate the reply back for display (#767).
    if locale:
        from terrapod.services import summary_translation

        content = await summary_translation.normalize_to_system_language(content, locale)

    try:
        assistant = await critic.post_critique_followup(db, ws.id, content)
    except FollowupDisabled as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except FollowupCapReached as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except FollowupBudgetExhausted as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except FollowupError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return JSONResponse(content={"data": await _translated_message_json(assistant, locale)})
