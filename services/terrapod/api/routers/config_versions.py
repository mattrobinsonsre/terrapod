"""Configuration version endpoints.

Most are TFE V2 compatible; the diff and ticket endpoints are
Terrapod extensions backing the workspace UI (they'll move under the
Terrapod-only namespace alongside the cleanup tracked in issue #269).

UX CONTRACT: consumed by `web/src/app/workspaces/[id]/page.tsx`
(Configurations tab). Changes to response shapes here MUST be
matched by frontend updates.

Endpoints:
    POST   /api/v2/workspaces/{id}/configuration-versions
    GET    /api/v2/workspaces/{id}/configuration-versions   (list, paginated)
    GET    /api/v2/configuration-versions/{cv_id}
    GET    /api/terrapod/v1/configuration-versions/{cv_id}/download  (tarball bytes)
    POST   /api/terrapod/v1/configuration-versions/{cv_id}/download-ticket
    GET    /api/terrapod/v1/configuration-versions/download-by-ticket/{ticket}
    POST   /api/terrapod/v1/configuration-versions/diff              (compare two CVs)
    PUT    /api/v2/configuration-versions/{cv_id}/upload    (tarball upload, no auth)
"""

import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth import download_tickets
from terrapod.db.models import ConfigurationVersion, Run, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import cv_diff_service, run_service
from terrapod.services.workspace_rbac_service import (
    has_permission,
    resolve_workspace_permission_for,
)
from terrapod.storage import get_storage
from terrapod.storage.keys import config_version_key
from terrapod.storage.protocol import ObjectNotFoundError

router = APIRouter(prefix="/api/v2", tags=["configuration-versions"])

# Terrapod-only extensions on the configuration-versions surface
# (download, download-ticket, download-by-ticket, diff). Dual-mounted
# under /api/terrapod/v1 (canonical) and /api/v2 (deprecated, removed
# in v0.24.0 — see #278).
extensions_router = APIRouter(tags=["configuration-version-extensions"])

logger = get_logger(__name__)

# TFE convention. CV lists can include the run that consumed them; we
# don't yet — but pagination defaults match what go-tfe expects.
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cv_json(cv: ConfigurationVersion) -> dict:
    """Serialize a ConfigurationVersion to TFE V2 JSON:API format."""
    from terrapod.config import settings

    base = settings.auth.callback_base_url.rstrip("/")
    cv_id = f"cv-{cv.id}"

    return {
        "data": {
            "id": cv_id,
            "type": "configuration-versions",
            "attributes": {
                "source": cv.source,
                "status": cv.status,
                "auto-queue-runs": cv.auto_queue_runs,
                "speculative": cv.speculative,
                "upload-url": f"{base}/api/v2/configuration-versions/{cv_id}/upload",
                "created-at": _rfc3339(cv.created_at),
            },
            "relationships": {
                "workspace": {
                    "data": {"id": f"ws-{cv.workspace_id}", "type": "workspaces"},
                },
            },
            "links": {
                "self": f"/api/v2/configuration-versions/{cv_id}",
            },
        }
    }


async def _get_workspace(workspace_id: str, db: AsyncSession) -> Workspace:
    ws_uuid = workspace_id.removeprefix("ws-")
    result = await db.execute(select(Workspace).where(Workspace.id == ws_uuid))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


@router.post("/workspaces/{workspace_id}/configuration-versions", status_code=201)
async def create_configuration_version(
    workspace_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a configuration version. Requires write on workspace."""
    ws = await _get_workspace(workspace_id, db)
    perm = await resolve_workspace_permission_for(db, user, ws)
    if not has_permission(perm, "write"):
        raise HTTPException(status_code=403, detail="Requires write permission on workspace")

    attrs = body.get("data", {}).get("attributes", {})

    cv = await run_service.create_configuration_version(
        db,
        workspace_id=ws.id,
        source=attrs.get("source", "tfe-api"),
        auto_queue_runs=attrs.get("auto-queue-runs", True),
        speculative=attrs.get("speculative", False),
    )
    await db.commit()
    await db.refresh(cv)

    return JSONResponse(content=_cv_json(cv), status_code=201)


@router.get("/configuration-versions/{cv_id}")
async def show_configuration_version(
    cv_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a configuration version."""
    cv_uuid = uuid.UUID(cv_id.removeprefix("cv-"))
    cv = await run_service.get_configuration_version(db, cv_uuid)
    if cv is None:
        raise HTTPException(status_code=404, detail="Configuration version not found")
    return JSONResponse(content=_cv_json(cv))


@router.get("/workspaces/{workspace_id}/configuration-versions")
async def list_configuration_versions(
    workspace_id: str = Path(...),
    page_size: int = Query(_DEFAULT_PAGE_SIZE, alias="page[size]", ge=1, le=_MAX_PAGE_SIZE),
    page_number: int = Query(1, alias="page[number]", ge=1),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List configuration versions for a workspace, newest first.

    Supports TFE-style pagination via `page[size]` + `page[number]`.
    RBAC: requires `read` on the workspace.
    """
    ws = await _get_workspace(workspace_id, db)
    perm = await resolve_workspace_permission_for(db, user, ws)
    if not has_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Workspace not found")

    total = await db.scalar(
        select(func.count())
        .select_from(ConfigurationVersion)
        .where(ConfigurationVersion.workspace_id == ws.id)
    )
    total = total or 0

    offset = (page_number - 1) * page_size
    result = await db.execute(
        select(ConfigurationVersion)
        .where(ConfigurationVersion.workspace_id == ws.id)
        .order_by(desc(ConfigurationVersion.created_at))
        .offset(offset)
        .limit(page_size)
    )
    cvs = list(result.scalars().all())

    # The "current" CV — bytes that the most recent successful apply
    # actually consumed. The UI uses this to badge a row in the list.
    # `applied` is the only terminal-success state we honour here;
    # `errored`/`canceled`/`discarded` runs may have referenced a CV
    # but they never landed in production state.
    current_cv_uuid = await db.scalar(
        select(Run.configuration_version_id)
        .where(
            Run.workspace_id == ws.id,
            Run.status == "applied",
            Run.configuration_version_id.isnot(None),
        )
        .order_by(desc(Run.apply_finished_at))
        .limit(1)
    )

    return JSONResponse(
        content={
            "data": [_cv_json(cv)["data"] for cv in cvs],
            "meta": {
                "pagination": {
                    "current-page": page_number,
                    "page-size": page_size,
                    "total-count": total,
                    "total-pages": (total + page_size - 1) // page_size if total else 0,
                },
                "current-id": f"cv-{current_cv_uuid}" if current_cv_uuid else None,
            },
        }
    )


@extensions_router.get("/configuration-versions/{cv_id}/download")
async def download_configuration_version(
    cv_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream a CV's tarball bytes back to the caller.

    RBAC: read on the owning workspace. 404 if the CV doesn't exist;
    409 if it hasn't been uploaded yet; 410 if retention has swept the
    bytes (the row stays after retention, the bytes don't).
    """
    cv_uuid = uuid.UUID(cv_id.removeprefix("cv-"))
    cv = await run_service.get_configuration_version(db, cv_uuid)
    if cv is None:
        raise HTTPException(status_code=404, detail="Configuration version not found")
    if cv.status != "uploaded":
        raise HTTPException(
            status_code=409,
            detail=f"Configuration version is in status {cv.status!r}, not yet uploaded",
        )

    # RBAC via the workspace this CV belongs to.
    ws = await db.get(Workspace, cv.workspace_id)
    if ws is None:
        # Workspace deleted out from under us — treat as gone.
        raise HTTPException(status_code=404, detail="Configuration version not found")
    perm = await resolve_workspace_permission_for(db, user, ws)
    if not has_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Configuration version not found")

    storage = get_storage()
    key = config_version_key(str(cv.workspace_id), str(cv.id))
    try:
        # `head` first so we can give a clean 410 when the bytes are
        # gone (retention swept) instead of the get_stream eventually
        # raising mid-response with a torn-off body.
        await storage.head(key)
    except ObjectNotFoundError as exc:
        raise HTTPException(
            status_code=410,
            detail=(
                "Configuration version tarball is no longer available "
                "(swept by retention). The version metadata remains for audit."
            ),
        ) from exc

    return StreamingResponse(
        storage.get_stream(key),
        media_type="application/x-tar",
        headers={
            "Content-Disposition": f'attachment; filename="{cv_id}.tar.gz"',
        },
    )


@extensions_router.post("/configuration-versions/{cv_id}/download-ticket")
async def mint_download_ticket(
    cv_id: str = Path(...),
    body: dict | None = Body(default=None),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Mint a short-lived HMAC ticket for browser-native downloading.

    Opt-in alternative to `/download` — the browser can't inject an
    Authorization header into plain navigation, so the ticket goes in
    the URL itself. Same auth gate as the regular download (read on
    the workspace), TTL-bounded, single-resource. See `download_tickets`
    module docstring for the cap-token design.
    """
    cv_uuid = uuid.UUID(cv_id.removeprefix("cv-"))
    cv = await run_service.get_configuration_version(db, cv_uuid)
    if cv is None:
        raise HTTPException(status_code=404, detail="Configuration version not found")
    if cv.status != "uploaded":
        raise HTTPException(
            status_code=409,
            detail=f"Configuration version is in status {cv.status!r}, not yet uploaded",
        )

    ws = await db.get(Workspace, cv.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Configuration version not found")
    perm = await resolve_workspace_permission_for(db, user, ws)
    if not has_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Configuration version not found")

    attrs = (body or {}).get("data", {}).get("attributes", {}) if body else {}
    requested_ttl = attrs.get("ttl-seconds", download_tickets.DEFAULT_TTL_SECONDS)
    try:
        requested_ttl = int(requested_ttl)
    except (TypeError, ValueError):
        requested_ttl = download_tickets.DEFAULT_TTL_SECONDS

    ticket = download_tickets.mint_ticket(
        resource_kind="cv",
        resource_id=str(cv.id),
        user_email=user.email,
        ttl_seconds=requested_ttl,
    )

    # Round-trip through verify so we surface the canonical (clamped)
    # expiry rather than re-deriving it from the requested TTL.
    payload = download_tickets.verify_ticket(ticket)
    assert payload is not None  # we just minted it
    expires_iso = datetime.fromtimestamp(payload.expires_at, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info(
        "Download ticket minted",
        cv_id=str(cv.id),
        user_email=user.email,
        ttl_seconds=payload.expires_at - int(time.time()),
    )

    return JSONResponse(
        content={
            "data": {
                "type": "download-tickets",
                "attributes": {
                    "ticket": ticket,
                    "url": f"/api/terrapod/v1/configuration-versions/download-by-ticket/{ticket}",
                    "expires-at": expires_iso,
                },
            }
        }
    )


@extensions_router.get("/configuration-versions/download-by-ticket/{ticket}")
async def download_by_ticket(
    ticket: str = Path(...),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream a CV tarball authenticated solely by the URL ticket.

    No `Authorization` header — the ticket IS the auth. Single-resource:
    a ticket minted for CV-X cannot be used to fetch CV-Y. Short-lived
    (default 5 min, max 30 min) so leaked URLs have minimal blast radius.
    """
    payload = download_tickets.verify_ticket(ticket)
    if payload is None or payload.resource_kind != "cv":
        raise HTTPException(status_code=401, detail="Invalid or expired download ticket")

    try:
        cv_uuid = uuid.UUID(payload.resource_id)
    except ValueError as exc:
        # HMAC verified but the id isn't a UUID — should be impossible
        # for a ticket we minted, but bail safely if a future kind reuses
        # this endpoint with a different id format.
        raise HTTPException(status_code=401, detail="Invalid download ticket") from exc

    cv = await run_service.get_configuration_version(db, cv_uuid)
    if cv is None:
        # CV was deleted between mint and use. Treat as gone — same shape
        # the regular download would surface.
        raise HTTPException(status_code=410, detail="Configuration version no longer exists")
    if cv.status != "uploaded":
        raise HTTPException(status_code=410, detail="Configuration version not uploaded")

    storage = get_storage()
    key = config_version_key(str(cv.workspace_id), str(cv.id))
    try:
        await storage.head(key)
    except ObjectNotFoundError as exc:
        raise HTTPException(
            status_code=410,
            detail="Configuration version tarball is no longer available (swept by retention)",
        ) from exc

    logger.info(
        "Configuration version downloaded via ticket",
        cv_id=str(cv.id),
        minter_email=payload.user_email,
    )

    return StreamingResponse(
        storage.get_stream(key),
        media_type="application/x-tar",
        headers={
            "Content-Disposition": f'attachment; filename="cv-{cv.id}.tar.gz"',
        },
    )


@extensions_router.post("/configuration-versions/diff")
async def diff_configuration_versions(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Compare the bytes of two configuration versions.

    Body:
        {"data": {"attributes": {"from-id": "cv-...", "to-id": "cv-..."}}}

    Returns a list of per-file diffs (added / removed / modified /
    binary-changed). Both CVs must be in the same workspace and the
    caller must have `read` on it. This endpoint is Terrapod-specific
    (not in TFE V2 spec); slated for the namespace cleanup in #269.
    """
    attrs = body.get("data", {}).get("attributes", {})
    from_id_raw = attrs.get("from-id", "")
    to_id_raw = attrs.get("to-id", "")
    if not from_id_raw or not to_id_raw:
        raise HTTPException(status_code=422, detail="`from-id` and `to-id` are required")

    try:
        from_uuid = uuid.UUID(from_id_raw.removeprefix("cv-"))
        to_uuid = uuid.UUID(to_id_raw.removeprefix("cv-"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid CV id") from exc

    from_cv = await run_service.get_configuration_version(db, from_uuid)
    to_cv = await run_service.get_configuration_version(db, to_uuid)
    if from_cv is None or to_cv is None:
        raise HTTPException(status_code=404, detail="Configuration version not found")

    if from_cv.workspace_id != to_cv.workspace_id:
        # Cross-workspace diffs would need RBAC checks on both sides
        # AND raise interesting questions about what the result means.
        # Refuse for now; revisit if a real use case shows up.
        raise HTTPException(
            status_code=422,
            detail="Both configuration versions must belong to the same workspace",
        )

    ws = await db.get(Workspace, from_cv.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Configuration version not found")
    perm = await resolve_workspace_permission_for(db, user, ws)
    if not has_permission(perm, "read"):
        raise HTTPException(status_code=404, detail="Configuration version not found")

    if from_cv.status != "uploaded" or to_cv.status != "uploaded":
        raise HTTPException(
            status_code=409,
            detail="Both configuration versions must be in `uploaded` status",
        )

    from_key = config_version_key(str(from_cv.workspace_id), str(from_cv.id))
    to_key = config_version_key(str(to_cv.workspace_id), str(to_cv.id))

    try:
        result = await cv_diff_service.diff_tarballs(from_key, to_key)
    except cv_diff_service.DiffTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ObjectNotFoundError as exc:
        raise HTTPException(
            status_code=410,
            detail=(
                "One or both configuration version tarballs are no longer "
                "available (swept by retention)."
            ),
        ) from exc

    return JSONResponse(
        content={
            "data": {
                "type": "configuration-version-diffs",
                "attributes": {
                    "from-id": f"cv-{from_cv.id}",
                    "to-id": f"cv-{to_cv.id}",
                    **result,
                },
            }
        }
    )


@router.put("/configuration-versions/{cv_id}/upload")
async def upload_configuration(
    request: Request,
    cv_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload configuration tarball.

    No auth required — the CV UUID acts as a capability token (same pattern
    as state version upload). go-tfe sends no Authorization header.
    """
    cv_uuid = uuid.UUID(cv_id.removeprefix("cv-"))
    cv = await run_service.get_configuration_version(db, cv_uuid)
    if cv is None:
        raise HTTPException(status_code=404, detail="Configuration version not found")

    if cv.status == "uploaded":
        raise HTTPException(status_code=409, detail="Configuration already uploaded")

    # Stream the tarball straight to storage — never buffer the whole thing in
    # the API's RAM. A monorepo configuration tarball can be hundreds of MB;
    # `await request.body()` here loads it all into one allocation and OOM-kills
    # the API pod (rule 14). storage.put_stream consumes the request body in
    # chunks and writes them straight through to the backend.
    storage = get_storage()
    key = config_version_key(str(cv.workspace_id), str(cv.id))
    meta = await storage.put_stream(key, request.stream(), content_type="application/x-tar")
    if meta.size_bytes == 0:
        await storage.delete(key)
        raise HTTPException(status_code=422, detail="Upload data is required")

    # Mark as uploaded
    cv = await run_service.mark_configuration_uploaded(db, cv)

    # Auto-queue runs if configured
    if cv.auto_queue_runs:
        # Find pending runs waiting for this config version
        result = await db.execute(
            select(Run).where(
                Run.configuration_version_id == cv.id,
                Run.status == "pending",
            )
        )
        pending_runs = result.scalars().all()
        for run in pending_runs:
            await run_service.queue_run(db, run)

    await db.commit()

    logger.info(
        "Configuration uploaded",
        cv_id=str(cv.id),
        size=meta.size_bytes,
    )

    return Response(status_code=200)
