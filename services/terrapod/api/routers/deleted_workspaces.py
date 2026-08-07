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
from terrapod.services.workspace_name import validate_workspace_name
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
            # Empty until this deletion has been restored at least once. A
            # second restore over the same lineage is the failure mode this
            # exists to make visible (#1299).
            "restored-to": [f"ws-{i}" for i in m.get("restored_to") or []],
            "restored-at": m.get("restored_at"),
            "restored-by": m.get("restored_by"),
        },
    }


def _raw_id(workspace_id: str) -> str:
    """Accept a workspace id in either form.

    Marker ids are bare UUIDs — the marker key is `state/deleted/{uuid}.json`,
    written before any serializer sees it — while every other workspace id in
    this API is `ws-`-prefixed. Rejecting the prefixed form made callers keep
    two spellings of the same id straight, and was conspicuous enough that the
    MCP tool description had to warn about it (#1299). Accepting both costs
    nothing; the emitted id stays bare so existing consumers are unaffected.
    """
    return workspace_id.removeprefix("ws-")


@router.get("/deleted-workspaces")
async def list_deleted_workspaces(
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Deleted workspaces still recoverable, newest deletion first."""
    storage = get_storage()
    markers = await dws.list_deleted(
        db, storage, settings.artifact_retention.deleted_workspace_retention_days
    )
    # Page FIRST, then count state versions: the count costs a full prefix
    # listing per marker, and paying it for every deleted workspace ever before
    # discarding all but one page is what made this endpoint scale with the
    # deployment's whole deletion history (#1299).
    page, meta = paginate(markers, request)
    await dws.attach_state_counts(storage, page)
    return {"data": [_marker_json(m) for m in page], "meta": meta}


@router.get("/deleted-workspaces/{workspace_id}")
async def get_deleted_workspace(
    workspace_id: str,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One deleted workspace's marker, with its recoverable state count."""
    marker = await dws.get_deleted(
        db,
        get_storage(),
        _raw_id(workspace_id),
        settings.artifact_retention.deleted_workspace_retention_days,
    )
    if marker is None:
        raise HTTPException(status_code=404, detail="Deleted workspace not found")
    return {"data": _marker_json(marker)}


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
    workspace_id = _raw_id(workspace_id)
    storage = get_storage()

    name = None
    force = False
    if isinstance(payload, dict):
        attrs = (payload.get("data") or {}).get("attributes") or {}
        force = bool(attrs.get("force"))
        raw = attrs.get("name")
        if raw is not None:
            if not isinstance(raw, str):
                raise HTTPException(status_code=422, detail="name must be a non-empty string")
            # The full format contract, not just "non-empty" (#1299). Every
            # other workspace-creating path enforces it, and the name is
            # load-bearing downstream: it is what the `cloud {}` block matches
            # on, what `/app/{org}/{name}` redirects by, and what appears in
            # the DR state index and VCS status contexts. Strict here because
            # this one came from a human who can simply retype it; the
            # marker-derived fallback sanitizes instead, so a corrupt marker
            # cannot make a recoverable workspace un-restorable.
            try:
                name = validate_workspace_name(raw)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e

    if not force:
        # A repeat restore yields a SECOND live workspace holding the same
        # lineage and serial over the same real infrastructure — after which an
        # apply in either makes the other's next plan read as wholesale drift.
        # Nothing else prevents it: the marker is not consumed and `_unique_name`
        # suffixes rather than conflicting, so it used to succeed silently
        # (#1299). Refuse by default and name what already exists; `force` is
        # there for the case where the first restore was itself discarded.
        existing = await dws.read_marker(storage, workspace_id)
        prior = dws.prior_restores(existing) if existing else []
        if prior:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This deletion has already been restored, into workspace(s) "
                    + ", ".join(f"ws-{i}" for i in prior)
                    + ". Restoring again would produce a second workspace holding "
                    "the same state lineage over the same infrastructure. Delete "
                    'the earlier restore first, or pass {"force": true} if it is '
                    "no longer live."
                ),
            )

    try:
        ws, report = await dws.restore_workspace(
            db,
            storage,
            workspace_id,
            restored_by=user.email,
            name=name,
            max_versions=settings.artifact_retention.state_versions_keep,
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
    # After the commit, so the marker never claims a restore that rolled back.
    await dws.record_restore(
        storage, workspace_id, new_workspace_id=str(ws.id), restored_by=user.email
    )
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
