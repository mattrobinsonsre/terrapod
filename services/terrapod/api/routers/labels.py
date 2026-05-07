"""Read-only labels browser API.

UX CONTRACT: consumed by `web/src/app/labels/page.tsx`. Changes to
response shapes here MUST be matched by frontend updates.

Endpoints:
    GET /api/v2/labels                       — distinct keys + per-type counts
    GET /api/v2/labels/{key}                 — distinct values for a key
    GET /api/v2/labels/{key}/{value}         — entities tagged with key=value

All three are RBAC-filtered: results only include labels carried by
entities the caller has at least `read` on for that entity's
permission model. Read-only by design — labels continue to be edited
on each entity's own edit page (no labels-admin surface here).
"""

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db
from terrapod.services import labels_service

router = APIRouter(prefix="/api/v2", tags=["labels"])


@router.get("/labels")
async def list_keys(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return all label keys in use across readable entities.

    Each key entry includes the count of distinct values and a
    breakdown of how many entities of each type carry that key.
    """
    keys = await labels_service.aggregate_keys(db, user)
    return JSONResponse(content={"data": keys})


@router.get("/labels/{key}")
async def list_values(
    key: str = Path(..., min_length=1, max_length=200),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return distinct values for `key`, with per-entity-type counts.

    Empty `data` is a valid response — means no readable entity
    carries this key (or no entity at all).
    """
    values = await labels_service.aggregate_values_for_key(db, user, key)
    return JSONResponse(content={"data": values})


@router.get("/labels/{key}/{value}")
async def list_entities(
    key: str = Path(..., min_length=1, max_length=200),
    value: str = Path(..., max_length=500),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return entities tagged with exactly `key=value`, grouped by type."""
    grouped = await labels_service.list_entities_for_label(db, user, key, value)
    return JSONResponse(content={"data": grouped})
