"""Undelete surface for deleted workspaces (#1253).

UX CONTRACT: consumed by `web/src/app/admin/deleted-workspaces/page.tsx`.
Changes to response shapes, attribute names, or status codes here MUST be
matched there.

**Platform admin only, on every route including the reads.** A delete marker
names a workspace, its labels and its variable *names*, and restoring one
materialises its state history — which is to say its secrets — into a workspace
the caller can then read. That is a privilege escalation for anyone who did not
have access to the original, so it is not delegated through workspace RBAC:
the original's ACL died with its rows and cannot be consulted.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.api.pagination import paginate
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import deleted_workspace_service as dws
from terrapod.storage import get_storage

logger = get_logger(__name__)

router = APIRouter(tags=["deleted-workspaces"])


def _marker_json(m: dict) -> dict:
    """Serialise a marker as a JSON:API resource.

    `variable-names` carries names and categories only — never values. That is
    an invariant of the marker itself (see `deleted_workspace_service`), so
    this is a passthrough rather than a filter; it is called out because a
    future field added to the marker must be checked before it lands here.
    """
    return {
        "id": m.get("workspace_id"),
        "type": "deleted-workspaces",
        "attributes": {
            "workspace-id": m.get("workspace_id"),
            "workspace-name": m.get("workspace_name"),
            "deleted-at": m.get("deleted_at"),
            "deleted-by": m.get("deleted_by"),
            "marker-reason": m.get("marker_reason"),
            "last-serial": m.get("last_serial"),
            "lineage": m.get("lineage"),
            "state-versions-available": m.get("state_versions_available"),
            "age-days": m.get("age_days"),
            "restorable-until": m.get("restorable_until"),
            "settings": m.get("settings") or {},
            "variable-names": m.get("variable_names") or [],
        },
    }


@router.get("/deleted-workspaces")
async def list_deleted_workspaces(
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Deleted workspaces still recoverable, newest deletion first."""
    markers = await dws.list_deleted(
        db, get_storage(), settings.artifact_retention.deleted_workspace_retention_days
    )
    page, meta = paginate([_marker_json(m) for m in markers], request)
    return {"data": page, "meta": meta}


@router.get("/deleted-workspaces/{workspace_id}")
async def get_deleted_workspace(
    workspace_id: str,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One deleted workspace's marker, with its recoverable state count."""
    markers = await dws.list_deleted(
        db, get_storage(), settings.artifact_retention.deleted_workspace_retention_days
    )
    for m in markers:
        if m.get("workspace_id") == workspace_id:
            return {"data": _marker_json(m)}
    raise HTTPException(status_code=404, detail="Deleted workspace not found")


@router.post("/deleted-workspaces/{workspace_id}/restore", status_code=201)
async def restore_deleted_workspace(
    workspace_id: str,
    payload: dict | None = None,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Recover a deleted workspace as a new workspace holding its state.

    This deliberately produces a **new** workspace with a new id rather than
    reviving the original in place. Re-attaching the original id would be
    cheaper — the state prefix is already there and no copy would be needed —
    but a frictionless one-click undo makes deletion feel free, and deletion
    should not feel free. Recovery is an explicit operation with a visible
    cost, and the result is honestly labelled as a new workspace.

    Returns the new workspace id plus a report of what was deliberately not
    carried over, so the operator knows what to re-enable.
    """
    name = None
    if isinstance(payload, dict):
        attrs = (payload.get("data") or {}).get("attributes") or {}
        raw = attrs.get("name")
        if raw is not None:
            if not isinstance(raw, str) or not raw.strip():
                raise HTTPException(status_code=422, detail="name must be a non-empty string")
            name = raw.strip()

    try:
        ws, report = await dws.restore_workspace(
            db, get_storage(), workspace_id, restored_by=user.email, name=name
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    if report["state_versions_restored"] == 0:
        # Nothing recoverable is a failed restore, not an empty success: the
        # state has already been reaped, or was never there. Rolling back
        # avoids leaving a bare workspace the operator then has to clean up
        # while believing their state came back.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "No state could be recovered for this workspace — it may already "
                "have passed its retention window and been reaped."
            ),
        )

    await db.commit()
    logger.info(
        "Restored deleted workspace",
        source_workspace_id=workspace_id,
        new_workspace_id=str(ws.id),
        new_workspace_name=ws.name,
        state_versions=report["state_versions_restored"],
        restored_by=user.email,
    )

    return {
        "data": {
            "id": f"ws-{ws.id}",
            "type": "workspaces",
            "attributes": {
                "name": ws.name,
                "restored-from": workspace_id,
                "state-versions-restored": report["state_versions_restored"],
                "state-versions-skipped": report["state_versions_skipped"],
                "suppressed": report["suppressed"],
                "dropped-references": report["dropped_references"],
            },
        }
    }
