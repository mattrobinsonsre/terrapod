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
"""

import hashlib
import os
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import (
    DEFAULT_ORG,
    AuthenticatedUser,
    get_current_user,
    require_non_runner,
)
from terrapod.db.models import Run, StateVersion, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import agent_pool_service as _agent_pool_service
from terrapod.services.pool_rbac_service import has_pool_permission, resolve_pool_permission
from terrapod.services.workspace_rbac_service import (
    PERMISSION_HIERARCHY,
    has_permission,
    resolve_workspace_permission,
)
from terrapod.storage import get_storage
from terrapod.storage.keys import state_index_key, state_key

router = APIRouter(prefix="/api/v2", tags=["tfe-v2"])
logger = get_logger(__name__)

TFP_API_VERSION = "2.6"
TFP_APP_NAME = "Terrapod"
X_TFE_VERSION = (
    "v202301-1"  # TFE monthly format; pre-202302 disables structured run output (unsupported)
)
TERRAPOD_VERSION = os.environ.get("TERRAPOD_VERSION", "dev")


def _primary_run_filter():
    """Filter out auxiliary runs that should not affect workspace health.

    Excludes module-test runs (module impact analysis) and speculative
    VCS PR/MR runs — these are informational and should not influence
    the workspace's displayed status.
    """
    return ~or_(
        Run.source == "module-test",
        and_(Run.plan_only.is_(True), Run.vcs_pull_request_number.isnot(None)),
    )


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


def _sanitize_working_directory(raw: str) -> str:
    """Sanitize working-directory: strip leading/trailing slashes, reject traversal."""
    v = raw.strip().strip("/")
    if ".." in v:
        raise HTTPException(status_code=422, detail="working-directory: path traversal not allowed")
    return v


def _validate_trigger_prefixes(raw: object) -> list[str]:
    """Validate and sanitize trigger-prefixes input.

    Each entry is normalized the same way as working-directory (strip slashes,
    reject traversal).  Max 20 entries.
    """
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="trigger-prefixes must be a list of strings")
    if len(raw) > 20:
        raise HTTPException(status_code=422, detail="trigger-prefixes: maximum 20 entries")
    result: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise HTTPException(status_code=422, detail="trigger-prefixes entries must be strings")
        v = _sanitize_working_directory(entry)
        if not v:
            raise HTTPException(
                status_code=422, detail="trigger-prefixes entries must be non-empty"
            )
        result.append(v)
    return result


_WORKSPACE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def _validate_workspace_name(name: str) -> str:
    """Validate and sanitize a workspace name.

    Rules:
    - Must start with alphanumeric
    - May contain alphanumeric, hyphens, underscores
    - Max 90 characters (matches DB column String(90))
    """
    cleaned = name.strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail="Workspace name is required")
    if len(cleaned) > 90:
        raise HTTPException(status_code=422, detail="Workspace name must be 90 characters or fewer")
    if not _WORKSPACE_NAME_RE.match(cleaned):
        raise HTTPException(
            status_code=422,
            detail="Workspace name must start with a letter or number and contain only letters, numbers, hyphens, and underscores",
        )
    return cleaned


def _clamp_drift_interval(value: int) -> int:
    """Clamp drift detection interval to the configured minimum."""
    from terrapod.config import settings

    return max(int(value), settings.drift_detection.min_workspace_interval_seconds)


async def _update_state_index(
    workspace_name: str,
    workspace_id: str,
    sv_key: str,
    serial: int,
) -> None:
    """Best-effort update of state/index.yaml with the latest state path.

    This index enables break-glass DR recovery: operators can download
    the index from object storage to find state files by workspace name
    without needing PostgreSQL access.

    Failures are logged and swallowed — index updates must never break
    state uploads.
    """
    try:
        import yaml

        storage = get_storage()
        idx_key = state_index_key()

        # Read existing index (or start fresh)
        try:
            raw = await storage.get(idx_key)
            index = yaml.safe_load(raw) or {}
        except Exception:
            index = {}

        index[workspace_name] = {
            "workspace_id": workspace_id,
            "state_key": sv_key,
            "serial": serial,
            "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

        await storage.put(
            idx_key,
            yaml.dump(index, default_flow_style=False).encode(),
            content_type="application/x-yaml",
        )
    except Exception:
        logger.warning("Failed to update state index", workspace=workspace_name, exc_info=True)


async def _remove_state_index_entry(workspace_name: str) -> None:
    """Best-effort remove a workspace entry from state/index.yaml."""
    try:
        import yaml

        storage = get_storage()
        idx_key = state_index_key()

        try:
            raw = await storage.get(idx_key)
            index = yaml.safe_load(raw) or {}
        except Exception:
            return  # No index to update

        if workspace_name in index:
            del index[workspace_name]
            await storage.put(
                idx_key,
                yaml.dump(index, default_flow_style=False).encode(),
                content_type="application/x-yaml",
            )
    except Exception:
        logger.warning(
            "Failed to remove state index entry", workspace=workspace_name, exc_info=True
        )


@router.get("/ping")
async def ping() -> JSONResponse:
    """TFE V2 API ping endpoint.

    Returns 200 OK with TFE-compatible headers. No auth required.
    Used by go-tfe client for initialization and version detection.
    """
    return JSONResponse(
        content={"app_name": TFP_APP_NAME, "version": TERRAPOD_VERSION},
        headers=_tfe_headers(),
    )


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


def _compute_health_conditions(ws: Workspace) -> list[dict]:
    """Compute all active health conditions from workspace DB fields."""
    conditions: list[dict] = []

    if ws.state_diverged:
        conditions.append(
            {
                "code": "state_diverged",
                "severity": "error",
                "title": "State may be diverged",
                "detail": "The last apply completed but the state upload failed. "
                "The actual infrastructure may not match the stored state.",
            }
        )

    if ws.execution_mode == "agent" and not ws.agent_pool_id:
        conditions.append(
            {
                "code": "no_agent_pool",
                "severity": "warning",
                "title": "No agent pool assigned",
                "detail": "This workspace is in agent execution mode but has no agent pool. "
                "Runs will be queued indefinitely because no runner can claim them.",
            }
        )

    if ws.vcs_last_error:
        conditions.append(
            {
                "code": "vcs_error",
                "severity": "error",
                "title": "VCS polling failed",
                "detail": ws.vcs_last_error,
            }
        )

    if ws.drift_detection_enabled and ws.drift_status == "drifted":
        conditions.append(
            {
                "code": "drifted",
                "severity": "warning",
                "title": "Infrastructure drift detected",
                "detail": "A drift detection run found changes between the stored state "
                "and the actual infrastructure.",
            }
        )

    if ws.drift_detection_enabled and ws.drift_status == "errored":
        conditions.append(
            {
                "code": "drift_errored",
                "severity": "warning",
                "title": "Drift detection errored",
                "detail": "The last drift detection run failed. Check the run output for details.",
            }
        )

    return conditions


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
                "operations": ws.execution_mode == "agent",
                "execution-backend": ws.execution_backend,
                "terraform-version": ws.terraform_version or "",
                "working-directory": ws.working_directory,
                "locked": ws.locked,
                "resource-cpu": ws.resource_cpu,
                "resource-memory": ws.resource_memory,
                "vcs-repo-url": ws.vcs_repo_url,
                "vcs-branch": ws.vcs_branch,
                "vcs-connection-id": f"vcs-{ws.vcs_connection_id}"
                if ws.vcs_connection_id
                else None,
                "var-files": ws.var_files or [],
                "trigger-prefixes": ws.trigger_prefixes or [],
                "drift-detection-enabled": ws.drift_detection_enabled,
                "drift-detection-interval-seconds": ws.drift_detection_interval_seconds,
                "drift-last-checked-at": _rfc3339(ws.drift_last_checked_at),
                "drift-status": ws.drift_status,
                "state-diverged": ws.state_diverged,
                "health-conditions": _compute_health_conditions(ws),
                "vcs-last-polled-at": _rfc3339(ws.vcs_last_polled_at),
                "vcs-last-error": ws.vcs_last_error,
                "vcs-last-error-at": _rfc3339(ws.vcs_last_error_at),
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


def _parse_tag_filters(request: Request) -> list[tuple[str, str | None]]:
    """Extract cloud-block tag filters from a workspace-list request.

    The terraform/tofu CLI's `cloud { workspaces { tags = ... } }` block emits two
    query-parameter shapes depending on whether `tags` is a list or a map:

      - list form  `tags = ["core", "env=prod"]`
            -> `?search[tags]=core,env=prod`
            (each comma-separated token is either a bare key or `key=value`)

      - map form   `tags = { env = "prod" }`
            -> `?filter[tagged][0][key]=env&filter[tagged][0][value]=prod`

    Terrapod doesn't have a separate "tags" concept on workspaces; instead each
    tag is matched against `Workspace.labels` (which is also the source of
    label-based RBAC). A bare key matches any workspace that has that label key
    set; `key=value` matches an exact label entry.

    Returns a list of `(key, value)` tuples where `value` is `None` for
    key-only matches.
    """
    filters: list[tuple[str, str | None]] = []

    # List form: search[tags]=a,b,c=d
    raw_tags = request.query_params.get("search[tags]", "")
    if raw_tags:
        for token in raw_tags.split(","):
            token = token.strip()
            if not token:
                continue
            if "=" in token:
                k, v = token.split("=", 1)
                filters.append((k.strip(), v.strip()))
            else:
                filters.append((token, None))

    # Map form: filter[tagged][N][key|value]=...
    indexed: dict[int, dict[str, str]] = {}
    for qk, qv in request.query_params.multi_items():
        m = re.match(r"^filter\[tagged\]\[(\d+)\]\[(key|value)\]$", qk)
        if not m:
            continue
        idx = int(m.group(1))
        indexed.setdefault(idx, {})[m.group(2)] = qv
    for idx in sorted(indexed):
        entry = indexed[idx]
        key = entry.get("key", "").strip()
        if not key:
            continue
        value = entry.get("value")
        filters.append((key, value.strip() if value is not None else None))

    return filters


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

    # Cloud-block tag filtering. Tags are matched against Workspace.labels —
    # see _parse_tag_filters for the dual-form parsing.
    if request is not None:
        for k, v in _parse_tag_filters(request):
            if v is None:
                query = query.where(Workspace.labels.has_key(k))  # noqa: W601
            else:
                query = query.where(Workspace.labels.contains({k: v}))

    result = await db.execute(query)
    workspaces = result.scalars().all()

    # Batch-load latest run per workspace using DISTINCT ON
    ws_ids = [ws.id for ws in workspaces]
    latest_runs: dict = {}
    if ws_ids:
        latest_run_q = (
            select(Run)
            .where(Run.workspace_id.in_(ws_ids), _primary_run_filter())
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

    # Load latest primary run for this workspace (excludes module-test / speculative PR runs)
    run_result = await db.execute(
        select(Run)
        .where(Run.workspace_id == ws.id, _primary_run_filter())
        .order_by(Run.created_at.desc())
        .limit(1)
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
    name = _validate_workspace_name(attrs.get("name", ""))

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

    # Resolve agent pool ID from attributes (requires write permission on pool)
    agent_pool_id = None
    pool_val = attrs.get("agent-pool-id")
    if pool_val:
        import uuid as _uuid

        agent_pool_id = _uuid.UUID(str(pool_val).removeprefix("apool-"))

        target_pool = await _agent_pool_service.get_pool(db, agent_pool_id)
        if target_pool is None:
            raise HTTPException(status_code=404, detail="Agent pool not found")
        pool_perm = await resolve_pool_permission(
            db,
            user_email=user.email,
            user_roles=user.roles,
            pool_name=target_pool.name,
            pool_labels=target_pool.labels or {},
            owner_email=target_pool.owner_email or "",
        )
        if pool_perm is None:
            raise HTTPException(status_code=404, detail="Agent pool not found")
        if not has_pool_permission(pool_perm, "write"):
            raise HTTPException(
                status_code=403,
                detail="Requires write permission on agent pool",
            )

    execution_mode = attrs.get("execution-mode", "local")
    if execution_mode not in ("local", "agent"):
        raise HTTPException(
            status_code=422,
            detail="execution-mode must be 'local' or 'agent'",
        )

    ws = Workspace(
        name=name,
        execution_mode=execution_mode,
        auto_apply=attrs.get("auto-apply", False),
        execution_backend=attrs.get("execution-backend", settings.default_execution_backend),
        terraform_version=attrs.get("terraform-version", settings.default_terraform_version),
        working_directory=_sanitize_working_directory(attrs.get("working-directory", "")),
        resource_cpu=attrs.get("resource-cpu", "1"),
        resource_memory=attrs.get("resource-memory", "2Gi"),
        labels=attrs.get("labels", {}),
        owner_email=user.email,
        agent_pool_id=agent_pool_id,
        vcs_connection_id=vcs_connection_id,
        vcs_repo_url=attrs.get("vcs-repo-url", ""),
        vcs_branch=attrs.get("vcs-branch", ""),
        var_files=_validate_var_files(attrs.get("var-files", [])),
        trigger_prefixes=_validate_trigger_prefixes(attrs.get("trigger-prefixes", [])),
        drift_detection_enabled=attrs.get(
            "drift-detection-enabled",
            True if vcs_connection_id else False,
        ),
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
    import uuid as _uuid

    ws_uuid = workspace_id.removeprefix("ws-")
    try:
        _uuid.UUID(ws_uuid)
    except ValueError:
        raise HTTPException(status_code=404, detail="Workspace not found") from None
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


@router.get("/workspaces/{workspace_id}")
async def show_workspace_by_id(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a workspace by its ID."""
    ws, perm = await _require_ws_permission(workspace_id, "read", user, db)

    # Load latest primary run for this workspace (excludes module-test / speculative PR runs)
    run_result = await db.execute(
        select(Run)
        .where(Run.workspace_id == ws.id, _primary_run_filter())
        .order_by(Run.created_at.desc())
        .limit(1)
    )
    latest_run = run_result.scalar_one_or_none()

    return JSONResponse(
        content=_workspace_json(ws, perm, latest_run=latest_run),
        headers=_tfe_headers(),
    )


@router.get("/workspaces/{workspace_id}/tag-bindings")
async def list_workspace_tag_bindings(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List a workspace's tag bindings.

    Terraform's cloud backend probes this endpoint to decide whether the TFE
    server supports key-value workspace tags. If it 404s, terraform falls back
    to assuming only key-only tags are supported and refuses to use map-form
    `tags = { key = "value" }` cloud blocks for state operations.

    Terrapod doesn't have a separate `tag_bindings` concept — workspace labels
    (also used for label-based RBAC) double as TFE tag bindings. Each label
    key/value pair is returned as one tag-binding entry.
    """
    ws, _ = await _require_ws_permission(workspace_id, "read", user, db)

    bindings = [
        {
            "type": "tag-bindings",
            "attributes": {
                "key": str(k),
                "value": str(v) if v is not None else "",
            },
        }
        for k, v in (ws.labels or {}).items()
    ]
    return JSONResponse(
        content={"data": bindings},
        headers=_tfe_headers(),
    )


@router.get("/workspaces/{workspace_id}/effective-tag-bindings")
async def list_workspace_effective_tag_bindings(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List a workspace's effective tag bindings.

    On HCP Terraform / TFE this includes tags inherited from a parent project.
    Terrapod has no project hierarchy, so this is identical to the
    workspace-level bindings.
    """
    ws, _ = await _require_ws_permission(workspace_id, "read", user, db)

    bindings = [
        {
            "type": "effective-tag-bindings",
            "attributes": {
                "key": str(k),
                "value": str(v) if v is not None else "",
            },
        }
        for k, v in (ws.labels or {}).items()
    ]
    return JSONResponse(
        content={"data": bindings},
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

    # Handle workspace rename
    old_name = None
    if "name" in attrs:
        new_name = _validate_workspace_name(attrs["name"])
        if new_name != ws.name:
            existing = await db.execute(
                select(Workspace).where(Workspace.name == new_name, Workspace.id != ws.id)
            )
            if existing.scalar_one_or_none() is not None:
                raise HTTPException(
                    status_code=422, detail=f"Workspace '{new_name}' already exists"
                )
            old_name = ws.name
            ws.name = new_name

    # owner-email can only be changed by platform admin
    if "owner-email" in attrs:
        if "admin" not in user.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only platform admins can change workspace owner",
            )
        ws.owner_email = attrs["owner-email"]

    if "execution-mode" in attrs:
        if attrs["execution-mode"] not in ("local", "agent"):
            raise HTTPException(
                status_code=422,
                detail="execution-mode must be 'local' or 'agent'",
            )
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
        ws.working_directory = _sanitize_working_directory(attrs["working-directory"])
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
    if "var-files" in attrs:
        ws.var_files = _validate_var_files(attrs["var-files"])
    if "trigger-prefixes" in attrs:
        ws.trigger_prefixes = _validate_trigger_prefixes(attrs["trigger-prefixes"])
    if "agent-pool-id" in attrs:
        import uuid as _uuid

        pool_val = attrs["agent-pool-id"]
        if pool_val is None:
            ws.agent_pool_id = None
        else:
            new_pool_id = _uuid.UUID(str(pool_val).removeprefix("apool-"))
            # Check write permission on target pool
            target_pool = await _agent_pool_service.get_pool(db, new_pool_id)
            if target_pool is None:
                raise HTTPException(status_code=404, detail="Agent pool not found")
            pool_perm = await resolve_pool_permission(
                db,
                user_email=user.email,
                user_roles=user.roles,
                pool_name=target_pool.name,
                pool_labels=target_pool.labels or {},
                owner_email=target_pool.owner_email or "",
            )
            if pool_perm is None:
                raise HTTPException(status_code=404, detail="Agent pool not found")
            if not has_pool_permission(pool_perm, "write"):
                raise HTTPException(
                    status_code=403,
                    detail="Requires write permission on agent pool",
                )
            ws.agent_pool_id = new_pool_id
    if "drift-detection-enabled" in attrs:
        ws.drift_detection_enabled = attrs["drift-detection-enabled"]
        # Reset drift status when disabling drift detection
        if not ws.drift_detection_enabled:
            ws.drift_status = ""
            ws.drift_last_checked_at = None
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
            # Auto-enable drift detection when VCS is connected (unless explicitly set in this request)
            if "drift-detection-enabled" not in attrs and ws.vcs_connection_id:
                ws.drift_detection_enabled = True

    await db.commit()
    await db.refresh(ws)

    from terrapod.redis.client import publish_workspace_event

    await publish_workspace_event(str(ws.id), "workspace_updated")

    # Update state index on rename
    if old_name is not None:
        await _remove_state_index_entry(old_name)
        latest_sv_result = await db.execute(
            select(StateVersion)
            .where(StateVersion.workspace_id == ws.id)
            .order_by(StateVersion.serial.desc())
            .limit(1)
        )
        sv = latest_sv_result.scalar_one_or_none()
        if sv:
            await _update_state_index(
                ws.name, str(ws.id), state_key(str(ws.id), str(sv.id)), sv.serial
            )
        logger.info("Workspace renamed", old_name=old_name, new_name=ws.name)

    return JSONResponse(content=_workspace_json(ws, perm), headers=_tfe_headers())


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a workspace and all associated resources. Requires admin."""
    ws, _ = await _require_ws_permission(workspace_id, "admin", user, db)
    ws_name = ws.name
    await db.delete(ws)
    await db.commit()
    logger.info("Workspace deleted", workspace=ws_name)

    # Best-effort remove from DR state index
    await _remove_state_index_entry(ws_name)

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
                "created-by": sv.created_by,
                "hosted-state-download-url": f"{base}/api/v2/state-versions/{sv_id}/download",
                "hosted-state-upload-url": f"{base}/api/v2/state-versions/{sv_id}/content",
                "hosted-json-state-upload-url": f"{base}/api/v2/state-versions/{sv_id}/json-content",
            },
            "relationships": {
                "run": {
                    "data": ({"id": f"run-{sv.run_id}", "type": "runs"} if sv.run_id else None),
                },
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

    # Extract run relationship if provided (go-tfe sends this when state
    # is created as part of a run)
    run_id_raw = (
        body.get("data", {}).get("relationships", {}).get("run", {}).get("data", {}).get("id", "")
    )
    run_uuid = None
    if run_id_raw:
        import uuid as _uuid

        try:
            run_uuid = _uuid.UUID(run_id_raw.removeprefix("run-"))
        except ValueError:
            pass  # ignore malformed run IDs

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
        created_by=user.email,
        run_id=run_uuid,
    )
    db.add(sv)
    await db.commit()
    await db.refresh(sv)

    from terrapod.api.metrics import STATE_VERSIONS_CREATED

    STATE_VERSIONS_CREATED.inc()

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

    # Best-effort update the DR state index
    if ws:
        await _update_state_index(ws.name, str(ws.id), key, sv.serial)

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
        from terrapod.api.metrics import STATE_LOCK_CONFLICTS

        STATE_LOCK_CONFLICTS.inc()
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
