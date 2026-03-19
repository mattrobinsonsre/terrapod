"""TFE V2 API compatibility endpoints.

These endpoints implement the minimum TFE V2 API surface needed for
terraform/tofu CLI and go-tfe client compatibility.

UX CONTRACT: Workspace endpoints are consumed by the web frontend:
  - web/src/app/workspaces/page.tsx (list, create)
  - web/src/app/workspaces/[id]/page.tsx (detail, update, delete, lock/unlock, state)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to those frontend pages.

Endpoints:
    GET  /api/v2/ping — API version handshake
    GET  /api/v2/account/details — current user info
    GET  /api/v2/organizations/default — organization details
    GET  /api/v2/organizations/default/entitlement-set — feature entitlements
    GET  /api/v2/organizations/default/workspaces — list workspaces
    GET  /api/v2/organizations/default/workspaces/{name} — workspace by name
    POST /api/v2/organizations/default/workspaces — create workspace
    GET  /api/v2/workspaces/{id} — workspace by ID
    PATCH /api/v2/workspaces/{id} — update workspace
    DELETE /api/v2/workspaces/{id} — delete workspace
    POST /api/v2/workspaces/{id}/actions/lock — lock workspace
    POST /api/v2/workspaces/{id}/actions/unlock — unlock workspace
    GET  /api/v2/workspaces/{id}/state-versions — list state versions
    GET  /api/v2/workspaces/{id}/current-state-version — latest state
    POST /api/v2/workspaces/{id}/state-versions — create state version
    GET  /api/v2/state-versions/{id} — show state version
    GET  /api/v2/state-versions/{id}/download — download raw state
    PUT  /api/v2/state-versions/{id}/content — upload raw state
    PUT  /api/v2/state-versions/{id}/json-content — upload JSON state
    GET  /api/v2/workspaces/{id}/vcs-refs — list VCS branches/tags for workspace
"""

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from terrapod.api.dependencies import (
    DEFAULT_ORG,
    AuthenticatedUser,
    get_current_user,
    require_non_runner,
)
from terrapod.db.models import Run, StateVersion, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.workspace_rbac_service import (
    PERMISSION_HIERARCHY,
    has_permission,
    resolve_workspace_permission,
)
from terrapod.storage import get_storage
from terrapod.storage.keys import state_key

router = APIRouter(prefix="/api/v2", tags=["tfe-v2"])
logger = get_logger(__name__)

TFP_API_VERSION = "2.6"
TFP_APP_NAME = "Terrapod"
X_TFE_VERSION = "v0.1.0"


def _rfc3339(dt: datetime | None) -> str:
    """Format a datetime as RFC3339 with trailing Z (what go-tfe expects)."""
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tfe_headers() -> dict[str, str]:
    return {
        "TFP-API-Version": TFP_API_VERSION,
        "TFP-AppName": TFP_APP_NAME,
        "X-TFE-Version": X_TFE_VERSION,
    }


_VAR_FILE_PATTERN = re.compile(r"^[\w./ -]+$")


def _validate_var_files(raw: object) -> list[str]:
    """Validate and sanitize var-files input.

    Rejects non-list types, non-string elements, path traversal, absolute
    paths, empty strings, and shell-unsafe characters.
    """
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="var-files must be a list of strings")
    if len(raw) > 20:
        raise HTTPException(status_code=422, detail="var-files: maximum 20 entries")
    result: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise HTTPException(status_code=422, detail="var-files entries must be strings")
        v = entry.strip()
        if not v:
            raise HTTPException(status_code=422, detail="var-files entries must be non-empty")
        if ".." in v or v.startswith("/"):
            raise HTTPException(status_code=422, detail=f"var-files: invalid path '{v}'")
        if not _VAR_FILE_PATTERN.match(v):
            raise HTTPException(
                status_code=422,
                detail=f"var-files: path contains invalid characters '{v}'",
            )
        result.append(v)
    return result


def _clamp_drift_interval(value: int) -> int:
    """Clamp drift detection interval to the configured minimum."""
    from terrapod.config import settings

    return max(int(value), settings.drift_detection.min_workspace_interval_seconds)


@router.get("/ping")
async def ping() -> JSONResponse:
    """TFE V2 API ping endpoint.

    Returns 200 OK with TFE-compatible headers. No auth required.
    Used by go-tfe client for initialization and version detection.
    """
    return JSONResponse(content={}, headers=_tfe_headers())


@router.get("/account/details")
async def account_details(
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """Return current user in JSON:API format matching TFE schema.

    Used by `terraform login` to verify the token works after creation.
    Also used by go-tfe client to determine the authenticated user.
    """
    # Use email prefix as username (TFE convention)
    username = user.email.split("@")[0] if user.email else ""

    return JSONResponse(
        content={
            "data": {
                "id": username,
                "type": "users",
                "attributes": {
                    "username": username,
                    "email": user.email,
                    "is-service-account": user.auth_method == "api_token",
                    "avatar-url": "",
                    "v2-only": False,
                    "permissions": {
                        "can-create-organizations": "admin" in user.roles,
                        "can-change-email": False,
                        "can-change-username": False,
                    },
                },
            }
        },
        headers=_tfe_headers(),
    )


# ── Organizations ────────────────────────────────────────────────────────────


@router.get("/organizations/default")
async def show_organization(
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """Return organization details in JSON:API format.

    Only the hardcoded "default" organization exists.
    """

    return JSONResponse(
        content={
            "data": {
                "id": DEFAULT_ORG,
                "type": "organizations",
                "attributes": {
                    "name": DEFAULT_ORG,
                    "external-id": DEFAULT_ORG,
                    "created-at": "2025-01-01T00:00:00.000Z",
                    "email": "",
                    "permissions": {
                        "can-update": "admin" in user.roles,
                        "can-destroy": False,
                        "can-access-via-teams": False,
                        "can-create-module": True,
                        "can-create-team": False,
                        "can-create-workspace": True,
                        "can-manage-users": "admin" in user.roles,
                        "can-manage-subscription": False,
                        "can-manage-sso": False,
                        "can-update-oauth": False,
                        "can-update-sentinel": False,
                        "can-update-ssh-keys": False,
                        "can-update-api-token": True,
                        "can-traverse": True,
                        "can-start-trial": False,
                        "can-update-agent-pools": "admin" in user.roles,
                        "can-manage-tags": True,
                        "can-manage-varsets": True,
                        "can-read-varsets": True,
                        "can-manage-public-providers": False,
                        "can-create-provider": True,
                        "can-manage-public-modules": False,
                        "can-manage-custom-providers": True,
                        "can-manage-run-tasks": True,
                        "can-read-run-tasks": True,
                    },
                },
            }
        },
        headers=_tfe_headers(),
    )


@router.get("/organizations/default/entitlement-set")
async def organization_entitlements(
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """Return feature entitlements for an organization.

    Enables all features — Terrapod is open source with no feature gating.
    """

    return JSONResponse(
        content={
            "data": {
                "id": DEFAULT_ORG,
                "type": "entitlement-sets",
                "attributes": {
                    "agents": True,
                    "audit-logging": True,
                    "configuration-designer": True,
                    "cost-estimation": True,
                    "global-run-tasks": True,
                    "operations": True,
                    "policy-enforcement": True,
                    "policy-limit": 0,
                    "policy-mandatory-enforcement-limit": 0,
                    "policy-set-limit": 0,
                    "private-module-registry": True,
                    "private-policy-agents": True,
                    "private-vcs": True,
                    "run-task-limit": 0,
                    "run-task-mandatory-enforcement-limit": 0,
                    "run-task-workspace-limit": 0,
                    "run-tasks": True,
                    "self-serve-billing": False,
                    "sentinel": False,
                    "sso": True,
                    "state-storage": True,
                    "teams": True,
                    "user-limit": 0,
                    "vcs-integrations": True,
                },
            }
        },
        headers=_tfe_headers(),
    )


# ── Workspaces ───────────────────────────────────────────────────────────────


def _workspace_json(
    ws: Workspace,
    effective_permission: str | None = None,
    latest_run: Run | None = None,
) -> dict:
    """Serialize a Workspace to TFE V2 JSON:API format.

    When effective_permission is provided, the permissions block reflects
    the user's actual permission level. Otherwise defaults to full access
    (for backwards compat with internal callers).
    """
    perm = effective_permission

    latest_run_attr = None
    if latest_run is not None:
        latest_run_attr = {
            "id": f"run-{latest_run.id}",
            "status": latest_run.status,
            "plan-only": latest_run.plan_only,
            "created-at": _rfc3339(latest_run.created_at),
        }

    return {
        "data": {
            "id": f"ws-{ws.id}",
            "type": "workspaces",
            "attributes": {
                "name": ws.name,
                "auto-apply": ws.auto_apply,
                "execution-mode": ws.execution_mode,
                "operations": ws.execution_mode == "remote",
                "execution-backend": ws.execution_backend,
                "terraform-version": ws.terraform_version or "",
                "working-directory": ws.working_directory,
                "locked": ws.locked,
                "resource-cpu": ws.resource_cpu,
                "resource-memory": ws.resource_memory,
                "vcs-repo-url": ws.vcs_repo_url,
                "vcs-branch": ws.vcs_branch,
                "vcs-working-directory": ws.vcs_working_directory,
                "vcs-connection-id": f"vcs-{ws.vcs_connection_id}"
                if ws.vcs_connection_id
                else None,
                "var-files": ws.var_files or [],
                "drift-detection-enabled": ws.drift_detection_enabled,
                "drift-detection-interval-seconds": ws.drift_detection_interval_seconds,
                "drift-last-checked-at": _rfc3339(ws.drift_last_checked_at),
                "drift-status": ws.drift_status,
                "state-diverged": ws.state_diverged,
                "latest-run": latest_run_attr,
                "agent-pool-id": f"apool-{ws.agent_pool_id}" if ws.agent_pool_id else None,
                "agent-pool-name": ws.agent_pool.name if ws.agent_pool else None,
                "vcs-connection-name": ws.vcs_connection.name if ws.vcs_connection else None,
                "labels": ws.labels or {},
                "owner-email": ws.owner_email,
                "created-at": _rfc3339(ws.created_at),
                "updated-at": _rfc3339(ws.updated_at),
                "permissions": {
                    "can-update": has_permission(perm, "admin"),
                    "can-destroy": has_permission(perm, "admin"),
                    "can-queue-run": has_permission(perm, "plan"),
                    "can-read-state-versions": has_permission(perm, "read"),
                    "can-create-state-versions": has_permission(perm, "write"),
                    "can-read-variable": has_permission(perm, "read"),
                    "can-update-variable": has_permission(perm, "write"),
                    "can-lock": has_permission(perm, "plan"),
                    "can-unlock": has_permission(perm, "plan"),
                    "can-force-unlock": has_permission(perm, "admin"),
                    "can-read-settings": has_permission(perm, "read"),
                },
                "actions": {
                    "is-destroyable": has_permission(perm, "admin"),
                },
            },
            "relationships": {
                "organization": {
                    "data": {"id": DEFAULT_ORG, "type": "organizations"},
                },
                **(
                    {
                        "vcs-connection": {
                            "data": {
                                "id": f"vcs-{ws.vcs_connection_id}",
                                "type": "vcs-connections",
                            },
                        },
                    }
                    if ws.vcs_connection_id
                    else {}
                ),
            },
        }
    }


@router.get("/organizations/default/workspaces")
async def list_workspaces(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
) -> JSONResponse:
    """List all workspaces (filtered by user permissions)."""

    query = select(Workspace).order_by(Workspace.name)

    # Support ?search[name]= filter
    search_name = request.query_params.get("search[name]", "") if request else ""
    if search_name:
        query = query.where(Workspace.name.ilike(f"%{search_name}%"))

    result = await db.execute(query)
    workspaces = result.scalars().all()

    # Batch-load latest run per workspace using DISTINCT ON
    ws_ids = [ws.id for ws in workspaces]
    latest_runs: dict = {}
    if ws_ids:
        latest_run_q = (
            select(Run)
            .where(Run.workspace_id.in_(ws_ids))
            .order_by(Run.workspace_id, Run.created_at.desc())
            .distinct(Run.workspace_id)
        )
        run_result = await db.execute(latest_run_q)
        for run in run_result.scalars().all():
            latest_runs[run.workspace_id] = run

    # Filter to workspaces user has at least read access to
    visible = []
    for ws in workspaces:
        perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
        if perm is not None:
            visible.append(_workspace_json(ws, perm, latest_run=latest_runs.get(ws.id))["data"])

    return JSONResponse(
        content={"data": visible},
        headers=_tfe_headers(),
    )


@router.get("/organizations/default/workspaces/{workspace_name}")
async def show_workspace(
    workspace_name: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a workspace by organization and name."""

    result = await db.execute(select(Workspace).where(Workspace.name == workspace_name))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if perm is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Load latest run for this workspace
    run_result = await db.execute(
        select(Run).where(Run.workspace_id == ws.id).order_by(Run.created_at.desc()).limit(1)
    )
    latest_run = run_result.scalar_one_or_none()

    return JSONResponse(
        content=_workspace_json(ws, perm, latest_run=latest_run),
        headers=_tfe_headers(),
    )


@router.post("/organizations/default/workspaces")
async def create_workspace(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_non_runner),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a workspace. Any authenticated user can create."""

    attrs = body.get("data", {}).get("attributes", {})
    name = attrs.get("name", "")
    if not name:
        raise HTTPException(status_code=422, detail="Workspace name is required")

    # Check for existing
    result = await db.execute(select(Workspace).where(Workspace.name == name))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=422, detail=f"Workspace '{name}' already exists")

    # Resolve VCS connection relationship
    vcs_connection_id = None
    relationships = body.get("data", {}).get("relationships", {})
    vcs_conn_data = relationships.get("vcs-connection", {}).get("data", {})
    if vcs_conn_data:
        vcs_conn_id_str = vcs_conn_data.get("id", "")
        if vcs_conn_id_str:
            import uuid as _uuid

            vcs_connection_id = _uuid.UUID(vcs_conn_id_str.removeprefix("vcs-"))

    from terrapod.config import settings

    # Resolve agent pool ID from attributes
    agent_pool_id = None
    pool_val = attrs.get("agent-pool-id")
    if pool_val:
        import uuid as _uuid

        agent_pool_id = _uuid.UUID(str(pool_val).removeprefix("apool-"))

    ws = Workspace(
        name=name,
        execution_mode=attrs.get("execution-mode", "local"),
        auto_apply=attrs.get("auto-apply", False),
        execution_backend=attrs.get("execution-backend", settings.default_execution_backend),
        terraform_version=attrs.get("terraform-version", settings.default_terraform_version),
        working_directory=attrs.get("working-directory", ""),
        resource_cpu=attrs.get("resource-cpu", "1"),
        resource_memory=attrs.get("resource-memory", "2Gi"),
        labels=attrs.get("labels", {}),
        owner_email=user.email,
        agent_pool_id=agent_pool_id,
        vcs_connection_id=vcs_connection_id,
        vcs_repo_url=attrs.get("vcs-repo-url", ""),
        vcs_branch=attrs.get("vcs-branch", ""),
        vcs_working_directory=attrs.get("vcs-working-directory", ""),
        var_files=_validate_var_files(attrs.get("var-files", [])),
        drift_detection_enabled=attrs.get("drift-detection-enabled", False),
        drift_detection_interval_seconds=_clamp_drift_interval(
            attrs.get("drift-detection-interval-seconds", 86400)
        ),
    )
    db.add(ws)
    await db.commit()
    await db.refresh(ws)

    logger.info("Workspace created", workspace=name, owner=user.email)
    return JSONResponse(
        content=_workspace_json(ws, "admin"),
        status_code=201,
        headers=_tfe_headers(),
    )


async def _get_workspace_by_id(workspace_id: str, db: AsyncSession) -> Workspace:
    """Look up a workspace by its ws-{uuid} ID."""
    ws_uuid = workspace_id.removeprefix("ws-")
    result = await db.execute(select(Workspace).where(Workspace.id == ws_uuid))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


async def _require_ws_permission(
    workspace_id: str,
    required: str,
    user: AuthenticatedUser,
    db: AsyncSession,
) -> tuple[Workspace, str]:
    """Load workspace and check permission. Returns (workspace, effective_permission)."""
    ws = await _get_workspace_by_id(workspace_id, db)
    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {required} permission on workspace",
        )
    return ws, perm


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


@router.get("/workspaces/{workspace_id}")
async def show_workspace_by_id(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a workspace by its ID."""
    ws, perm = await _require_ws_permission(workspace_id, "read", user, db)

    # Load latest run for this workspace
    run_result = await db.execute(
        select(Run).where(Run.workspace_id == ws.id).order_by(Run.created_at.desc()).limit(1)
    )
    latest_run = run_result.scalar_one_or_none()

    return JSONResponse(
        content=_workspace_json(ws, perm, latest_run=latest_run),
        headers=_tfe_headers(),
    )


@router.patch("/workspaces/{workspace_id}")
async def update_workspace(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Update workspace settings. Requires admin on workspace."""
    ws, perm = await _require_ws_permission(workspace_id, "admin", user, db)

    attrs = body.get("data", {}).get("attributes", {})

    # owner-email can only be changed by platform admin
    if "owner-email" in attrs:
        if "admin" not in user.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only platform admins can change workspace owner",
            )
        ws.owner_email = attrs["owner-email"]

    if "execution-mode" in attrs:
        ws.execution_mode = attrs["execution-mode"]
    if "auto-apply" in attrs:
        ws.auto_apply = attrs["auto-apply"]
    if "execution-backend" in attrs:
        backend = attrs["execution-backend"]
        if backend not in ("terraform", "tofu"):
            raise HTTPException(
                status_code=422,
                detail="execution-backend must be 'terraform' or 'tofu'",
            )
        ws.execution_backend = backend
    if "terraform-version" in attrs:
        ws.terraform_version = attrs["terraform-version"]
    if "working-directory" in attrs:
        ws.working_directory = attrs["working-directory"]
    if "resource-cpu" in attrs:
        ws.resource_cpu = attrs["resource-cpu"]
    if "resource-memory" in attrs:
        ws.resource_memory = attrs["resource-memory"]
    if "labels" in attrs:
        new_labels = attrs["labels"]
        # Self-lockout check: warn if label change would reduce user's access
        # Platform admins and owners are immune (their access doesn't depend on labels)
        if (
            new_labels != (ws.labels or {})
            and not attrs.get("force")
            and "admin" not in user.roles
            and ws.owner_email != user.email
        ):
            old_labels = ws.labels
            ws.labels = new_labels
            new_perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
            ws.labels = old_labels  # revert before deciding
            if new_perm is None or PERMISSION_HIERARCHY.get(
                new_perm, -1
            ) < PERMISSION_HIERARCHY.get(perm, -1):
                new_level = new_perm or "none"
                return JSONResponse(
                    status_code=409,
                    content={
                        "errors": [
                            {
                                "status": "409",
                                "title": "Label change would reduce your access",
                                "detail": (
                                    f"This label change would reduce your access from "
                                    f"{perm} to {new_level} on this workspace. "
                                    f'Re-submit with "force": true to confirm.'
                                ),
                            }
                        ]
                    },
                )
        ws.labels = new_labels
    if "vcs-repo-url" in attrs:
        ws.vcs_repo_url = attrs["vcs-repo-url"]
    if "vcs-branch" in attrs:
        ws.vcs_branch = attrs["vcs-branch"]
    if "vcs-working-directory" in attrs:
        ws.vcs_working_directory = attrs["vcs-working-directory"]
    if "var-files" in attrs:
        ws.var_files = _validate_var_files(attrs["var-files"])
    if "agent-pool-id" in attrs:
        import uuid as _uuid

        pool_val = attrs["agent-pool-id"]
        if pool_val is None:
            ws.agent_pool_id = None
        else:
            ws.agent_pool_id = _uuid.UUID(str(pool_val).removeprefix("apool-"))
    if "drift-detection-enabled" in attrs:
        ws.drift_detection_enabled = attrs["drift-detection-enabled"]
    if "drift-detection-interval-seconds" in attrs:
        ws.drift_detection_interval_seconds = _clamp_drift_interval(
            attrs["drift-detection-interval-seconds"]
        )

    # VCS connection relationship
    relationships = body.get("data", {}).get("relationships", {})
    if "vcs-connection" in relationships:
        vcs_conn_data = relationships["vcs-connection"].get("data")
        if vcs_conn_data is None:
            # Explicit null = disconnect VCS
            ws.vcs_connection_id = None
        else:
            import uuid as _uuid

            vcs_id = vcs_conn_data.get("id", "")
            ws.vcs_connection_id = _uuid.UUID(vcs_id.removeprefix("vcs-")) if vcs_id else None

    await db.commit()
    await db.refresh(ws)

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "workspace_updated")

    return JSONResponse(content=_workspace_json(ws, perm), headers=_tfe_headers())


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a workspace and all associated resources. Requires admin."""
    ws, _ = await _require_ws_permission(workspace_id, "admin", user, db)
    await db.delete(ws)
    await db.commit()
    logger.info("Workspace deleted", workspace=ws.name)
    return Response(status_code=204)


# ── State Versions ───────────────────────────────────────────────────────────


@router.get("/workspaces/{workspace_id}/state-versions")
async def list_state_versions(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all state versions for a workspace, ordered by serial DESC."""
    ws, _ = await _require_ws_permission(workspace_id, "read", user, db)

    result = await db.execute(
        select(StateVersion)
        .where(StateVersion.workspace_id == ws.id)
        .order_by(StateVersion.serial.desc())
    )
    state_versions = result.scalars().all()

    return JSONResponse(
        content={
            "data": [_state_version_json(sv)["data"] for sv in state_versions],
        },
        headers=_tfe_headers(),
    )


@router.get("/workspaces/{workspace_id}/current-state-version")
async def current_state_version(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Get the current (latest) state version for a workspace."""
    ws, _ = await _require_ws_permission(workspace_id, "read", user, db)

    result = await db.execute(
        select(StateVersion)
        .where(StateVersion.workspace_id == ws.id)
        .order_by(StateVersion.serial.desc())
        .limit(1)
    )
    sv = result.scalar_one_or_none()
    if sv is None:
        raise HTTPException(status_code=404, detail="No state versions found")

    return JSONResponse(content=_state_version_json(sv), headers=_tfe_headers())


@router.get("/state-versions/{state_version_id}/download")
async def download_state(
    state_version_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Download the raw state JSON for a state version. Requires plan permission."""
    sv_uuid = state_version_id.removeprefix("sv-")

    result = await db.execute(select(StateVersion).where(StateVersion.id == sv_uuid))
    sv = result.scalar_one_or_none()
    if sv is None:
        raise HTTPException(status_code=404, detail="State version not found")

    # Check plan permission on the workspace (raw state may contain secrets)
    ws = await db.get(Workspace, sv.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, "plan"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires plan permission on workspace",
        )

    storage = get_storage()
    key = state_key(str(sv.workspace_id), str(sv.id))
    try:
        data = await storage.get(key)
    except Exception:
        raise HTTPException(status_code=404, detail="State data not yet uploaded") from None

    return Response(content=data, media_type="application/json")


def _state_version_json(sv: StateVersion) -> dict:
    """Serialize a StateVersion to TFE V2 JSON:API format.

    Uses callback_base_url for absolute URLs (go-tfe requires absolute URLs
    for hosted-state-upload-url).
    """
    from terrapod.config import settings

    base = settings.auth.callback_base_url.rstrip("/")
    sv_id = f"sv-{sv.id}"
    return {
        "data": {
            "id": sv_id,
            "type": "state-versions",
            "attributes": {
                "serial": sv.serial,
                "lineage": sv.lineage,
                "md5": sv.md5,
                "size": sv.state_size,
                "created-at": _rfc3339(sv.created_at),
                "hosted-state-download-url": f"{base}/api/v2/state-versions/{sv_id}/download",
                "hosted-state-upload-url": f"{base}/api/v2/state-versions/{sv_id}/content",
                "hosted-json-state-upload-url": f"{base}/api/v2/state-versions/{sv_id}/json-content",
            },
            "links": {
                "self": f"/api/v2/state-versions/{sv_id}",
                "download": f"/api/v2/state-versions/{sv_id}/download",
            },
        }
    }


@router.get("/state-versions/{state_version_id}")
async def show_state_version(
    state_version_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a state version by ID.

    go-tfe reads this to get hosted-state-upload-url before uploading.
    """
    sv_uuid = state_version_id.removeprefix("sv-")
    result = await db.execute(select(StateVersion).where(StateVersion.id == sv_uuid))
    sv = result.scalar_one_or_none()
    if sv is None:
        raise HTTPException(status_code=404, detail="State version not found")

    # Check read permission on workspace
    ws = await db.get(Workspace, sv.workspace_id)
    if ws is not None:
        perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
        if perm is None:
            raise HTTPException(status_code=404, detail="State version not found")

    return JSONResponse(content=_state_version_json(sv), headers=_tfe_headers())


@router.post("/workspaces/{workspace_id}/state-versions")
async def create_state_version(
    request: Request,
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a new state version. Requires write permission."""
    ws, _ = await _require_ws_permission(workspace_id, "write", user, db)

    body = await request.json()
    attrs = body.get("data", {}).get("attributes", {})

    serial = attrs.get("serial", 0)
    lineage = attrs.get("lineage", "")
    md5_from_client = attrs.get("md5", "")
    force = attrs.get("force", False)

    # Check for serial conflict (unless force is set)
    if not force:
        existing = await db.execute(
            select(StateVersion).where(
                StateVersion.workspace_id == ws.id,
                StateVersion.serial == serial,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=409, detail="State version serial already exists")

    sv = StateVersion(
        workspace_id=ws.id,
        serial=serial,
        lineage=lineage,
        md5=md5_from_client,
        state_size=0,
    )
    db.add(sv)
    await db.commit()
    await db.refresh(sv)

    logger.info("State version created", workspace=ws.name, serial=serial, sv_id=str(sv.id))

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "state_version_created")

    return JSONResponse(
        content=_state_version_json(sv),
        status_code=201,
        headers=_tfe_headers(),
    )


@router.put("/state-versions/{state_version_id}/content")
async def upload_state_content(
    request: Request,
    state_version_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload raw state JSON for a state version.

    Called by go-tfe after creating the state version record.
    No auth required — go-tfe uses presigned-style uploads without
    Authorization header. The state version UUID acts as a capability token.
    """
    sv_uuid = state_version_id.removeprefix("sv-")

    result = await db.execute(select(StateVersion).where(StateVersion.id == sv_uuid))
    sv = result.scalar_one_or_none()
    if sv is None:
        raise HTTPException(status_code=404, detail="State version not found")

    state_data = await request.body()
    if not state_data:
        raise HTTPException(status_code=422, detail="State data is required")

    # Store in object storage (encryption at rest delegated to storage backend)
    storage = get_storage()
    key = state_key(str(sv.workspace_id), str(sv.id))
    await storage.put(key, state_data, content_type="application/octet-stream")

    # Update metadata
    sv.state_size = len(state_data)
    sv.md5 = hashlib.md5(state_data).hexdigest()  # nosemgrep: insecure-hash-algorithm-md5

    # Clear state_diverged flag on successful state upload
    ws = await db.get(Workspace, sv.workspace_id)
    if ws and ws.state_diverged:
        ws.state_diverged = False

    await db.commit()

    logger.info("State content uploaded", sv_id=str(sv.id), size=len(state_data))

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(sv.workspace_id), "state_version_created")

    return Response(status_code=200)


@router.put("/state-versions/{state_version_id}/json-content")
async def upload_json_state_content(
    request: Request,
    state_version_id: str = Path(...),
) -> Response:
    """Upload JSON state representation for a state version.

    go-tfe uploads this alongside the raw state. No auth required
    (same as /content — go-tfe uses presigned-style uploads).
    For now we accept and discard it.
    """
    await request.body()  # consume the body
    return Response(status_code=200)


@router.post("/workspaces/{workspace_id}/actions/lock")
async def lock_workspace(
    request: Request,
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Lock a workspace. Requires plan permission."""
    ws, perm = await _require_ws_permission(workspace_id, "plan", user, db)

    # Parse lock info from request body
    import json as json_mod

    try:
        raw = await request.body()
        lock_info = json_mod.loads(raw) if raw else {}
    except (json_mod.JSONDecodeError, ValueError):
        lock_info = {}

    lock_id = lock_info.get("ID", f"lock-{user.email}")

    if ws.locked:
        raise HTTPException(
            status_code=409, detail=f'workspace already locked (lock ID: "{ws.lock_id}")'
        )

    ws.locked = True
    ws.lock_id = lock_id
    await db.commit()
    await db.refresh(ws)

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "workspace_lock_change", {"locked": True})

    return JSONResponse(content=_workspace_json(ws, perm), headers=_tfe_headers())


@router.post("/workspaces/{workspace_id}/actions/unlock")
async def unlock_workspace(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Unlock a workspace. Plan for own lock, admin for force-unlock."""
    ws = await _get_workspace_by_id(workspace_id, db)
    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)

    # Check: at minimum plan permission required
    if not has_permission(perm, "plan"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires plan permission on workspace",
        )

    # If locked by someone else, require admin to force-unlock
    own_lock = ws.lock_id and (ws.lock_id == f"lock-{user.email}")
    if ws.locked and not own_lock and not has_permission(perm, "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires admin permission to force-unlock workspace locked by another user",
        )

    ws.locked = False
    ws.lock_id = None
    await db.commit()
    await db.refresh(ws)

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "workspace_lock_change", {"locked": False})

    return JSONResponse(content=_workspace_json(ws, perm), headers=_tfe_headers())


@router.get("/workspaces/{workspace_id}/vcs-refs")
async def list_vcs_refs(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List branches, tags, and default branch for a VCS-connected workspace.

    Requires read permission on the workspace.
    """
    from terrapod.db.models import VCSConnection
    from terrapod.services.vcs_poller import (
        _list_branches,
        _list_tags,
        _parse_repo_url,
        _resolve_branch,
    )

    ws = await _get_workspace_by_id(workspace_id, db)
    perm = await resolve_workspace_permission(db, user.email, user.roles, ws)
    if not has_permission(perm, "read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires read permission on workspace",
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
