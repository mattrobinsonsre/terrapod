"""Cost-estimation endpoints (#871) — Terrapod-native (`/api/terrapod/v1/`).

Serves the cached OpenInfraQuote pricesheet to runners (the plan-path cost
phase fetches it, like the binary cache) and exposes admin refresh/status. This
is a Terrapod extension, not part of the `terraform`/`tofu` CLI surface, so it
lives under `/api/terrapod/v1/`, never `/api/v2/`.

Cost estimates are powered by OpenInfraQuote
(https://github.com/terrateamio/openinfraquote, MPL-2.0).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse

from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_admin
from terrapod.config import settings
from terrapod.services import cost_pricesheet_service
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
        "source": "OpenInfraQuote (https://github.com/terrateamio/openinfraquote)",
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
