"""Cost-estimation endpoints (#871) — Terrapod-native (`/api/terrapod/v1/`).

Serves the cached pricesheet to runners (the plan-path cost
phase fetches it, like the binary cache) and exposes admin refresh/status. This
is a Terrapod extension, not part of the `terraform`/`tofu` CLI surface, so it
lives under `/api/terrapod/v1/`, never `/api/v2/`.

Cost estimates are computed by Terrapod's native cost engine over its own
self-generated pricesheet (see :mod:`terrapod.services.cost`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_admin
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.services import cost_pricesheet_service, workspace_cost_service
from terrapod.storage import get_storage
from terrapod.storage.protocol import ObjectStore

router = APIRouter(tags=["cost-estimation"])

_DISABLED_DETAIL = "Cost estimation is disabled"
_NOT_CACHED_DETAIL = "Pricesheet not cached yet — trigger a refresh or pre-seed it"


@router.get("/cost-estimation/pricesheet")
async def download_pricesheet(
    user: AuthenticatedUser = Depends(get_current_user),
    storage: ObjectStore = Depends(get_storage),
) -> RedirectResponse:
    """Redirect (302) to a presigned URL for the cached pricesheet CSV.

    Consumed by runner Jobs (plan-path cost phase). Pull-through: the sheet is
    fetched on demand if the cached copy is missing or stale (no schedule), and
    a stale copy is served if a refresh fails. Requires authentication (runner
    token, API token, or session) — the returned presigned URL does not.
    """
    if not settings.cost_estimation.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_DISABLED_DETAIL)
    if not await cost_pricesheet_service.ensure_pricesheet(storage):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_CACHED_DETAIL)
    url = await cost_pricesheet_service.pricesheet_download_url(storage)
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/workspaces/{workspace_id}/cost-estimate")
async def show_workspace_cost_estimate(
    workspace_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Current monthly cost of a workspace's managed infrastructure (#871).

    Terrapod-native. Runs the workspace's latest state version through the native
    native cost engine server-side and returns the estimate — the
    *state* analogue of the run Cost tab's plan-*delta* estimate. Same
    ``currency/total/previous/diff/resources/unpriced`` shape, plus a
    ``state-version`` meta naming the priced version (null when the workspace has
    no state yet). Every figure is **data** (engine-derived); no AI is involved.
    Gated on ``state:read`` (the estimate derives from the state blob). 404 when
    cost estimation is disabled; 503 when no pricesheet is available.
    """
    if not settings.cost_estimation.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_DISABLED_DETAIL)
    try:
        attrs = await workspace_cost_service.estimate_workspace_cost(db, user, workspace_id)
    except workspace_cost_service.PricesheetUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    ws_short = workspace_id.removeprefix("ws-")
    return JSONResponse(
        content={
            "data": {
                "id": f"workspace-cost-{ws_short}",
                "type": "workspace-cost-estimates",
                "attributes": attrs,
                "relationships": {
                    "workspace": {"data": {"id": f"ws-{ws_short}", "type": "workspaces"}},
                },
            }
        }
    )


@router.get("/cost-estimation/pricesheet/status")
async def pricesheet_status(
    user: AuthenticatedUser = Depends(require_admin),
    storage: ObjectStore = Depends(get_storage),
) -> dict:
    """Report whether cost estimation is enabled and the pricesheet is cached."""
    available = (
        settings.cost_estimation.enabled
        and await cost_pricesheet_service.pricesheet_available(storage)
    )
    return {
        "enabled": settings.cost_estimation.enabled,
        "available": available,
        "prices_url": settings.cost_estimation.prices_url,
        "source": "Terrapod self-generated pricesheet (pricegen)",
    }


@router.post("/cost-estimation/pricesheet/refresh")
async def refresh_pricesheet(
    user: AuthenticatedUser = Depends(require_admin),
    storage: ObjectStore = Depends(get_storage),
) -> dict:
    """Force a re-fetch of the pricesheet into object storage (admin only)."""
    if not settings.cost_estimation.enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_DISABLED_DETAIL)
    try:
        size = await cost_pricesheet_service.refresh_pricesheet(storage)
    except Exception as exc:  # noqa: BLE001 - surface upstream/decompress failures as 502
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Pricesheet refresh failed: {exc}",
        ) from exc
    return {"refreshed": True, "size_bytes": size}
