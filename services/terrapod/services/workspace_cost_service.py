"""Workspace state-cost service (#871).

Prices a workspace's **current managed infrastructure** by running its latest
Terraform state through the native cost engine
(:mod:`terrapod.services.cost`) — the state analogue of the runner's plan-cost
phase (:mod:`terrapod.runner.phases.cost`), which prices a plan *delta*. This
powers the workspace cost card (current monthly managed-infra cost).

Gated on ``state:read`` (the same as the state graph): the estimate is derived
from the secret-bearing state blob, even though only non-sensitive aggregates
(resource addresses, types, monthly cost) are returned. The heavy work — state
decrypt + parse, and streaming a ~200k-row pricesheet — runs via
``asyncio.to_thread`` (rule 13); the pricesheet lands on the attached PVC, never
``/tmp`` (rule 14). A workspace with no state yet is a normal condition, not an
error: it returns a zeroed estimate with ``state_version = None``.

The estimate is computed by Terrapod's native cost engine over its own
self-generated pricesheet.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.config import settings
from terrapod.db.models import StateVersion, Workspace
from terrapod.services import cost_pricesheet_service
from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for
from terrapod.storage import get_storage
from terrapod.storage.keys import state_key

logger = structlog.get_logger(__name__)


class PricesheetUnavailable(Exception):
    """No cost pricesheet is cached and an on-demand fetch failed."""


def _rfc3339(dt) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _empty_attrs(state_version: dict | None = None) -> dict[str, Any]:
    """A well-formed, zeroed estimate — used when the workspace has no state."""
    zero = {"min": 0.0, "max": 0.0}
    return {
        "currency": "USD",
        "total": dict(zero),
        "previous": dict(zero),
        "diff": dict(zero),
        "resources": [],
        "unpriced": [],
        "state-version": state_version,
    }


def _run_engine(state_bytes: bytes, pricesheet_path: str, default_region: str) -> dict[str, Any]:
    """Parse state + run the cost engine (sync — called via ``to_thread``).

    ``pricesheet_path`` is the cached **SQLite index** (#1034); the engine queries
    it off disk (bounded memory) instead of parsing the whole sheet.
    """
    from terrapod.services.cost import estimate
    from terrapod.services.cost.pricesheet_db import PricesheetIndex

    tf_json = json.loads(state_bytes)
    index = PricesheetIndex.open(pricesheet_path)
    try:
        result = estimate(tf_json, index=index, default_region=default_region)
    finally:
        index.close()
    return result.to_dict()


async def estimate_workspace_cost(db: AsyncSession, user, workspace_id: str) -> dict[str, Any]:
    """Estimate the current monthly cost of a workspace's managed infrastructure.

    Resolves the workspace, enforces ``state:read``, runs its latest state
    version through the cost engine, and returns the estimate attributes (the
    same ``currency/total/previous/diff/resources/unpriced`` shape the run cost
    tab uses, plus a ``state-version`` meta identifying the priced version).

    A workspace with no state (or a state-version row whose bytes haven't landed
    or were swept) returns a zeroed estimate with ``state-version = None`` rather
    than erroring. Raises :class:`PricesheetUnavailable` when no pricesheet is
    cached and an on-demand refresh fails, and ``HTTPException(403)`` when the
    caller lacks ``state:read``.
    """
    from terrapod.api.routers.tfe_v2 import _get_workspace_by_id

    ws: Workspace = await _get_workspace_by_id(workspace_id, db)

    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.STATE_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires state:read permission on workspace",
        )

    sv = (
        await db.execute(
            select(StateVersion)
            .where(StateVersion.workspace_id == ws.id)
            .order_by(StateVersion.serial.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # No state yet, or a version row written before its /content PUT landed.
    if sv is None or sv.state_size == 0:
        return _empty_attrs(None)

    sv_meta = {
        "id": f"sv-{sv.id}",
        "serial": sv.serial,
        "created-at": _rfc3339(sv.created_at),
    }

    storage = get_storage()
    try:
        raw = await storage.get(state_key(str(ws.id), str(sv.id)))
    except Exception:  # noqa: BLE001 - metadata row without a backing object
        logger.warning("workspace_cost_state_blob_missing", state_version_id=str(sv.id))
        return _empty_attrs(sv_meta)

    from terrapod.crypto.state import decrypt_state_bytes

    state_bytes = await decrypt_state_bytes(raw)

    # A cold/stale cache is fetched on demand here (pull-through); a total
    # failure with nothing cached is surfaced honestly rather than silently
    # pricing everything as $0.
    if not await cost_pricesheet_service.ensure_pricesheet(storage):
        raise PricesheetUnavailable(
            "Cost pricesheet is not available (upstream fetch failed and nothing cached)"
        )

    pricesheet_path = await cost_pricesheet_service.download_cached_to_file(storage)
    try:
        default_region = settings.cost_estimation.default_region
        attrs = await asyncio.to_thread(_run_engine, state_bytes, pricesheet_path, default_region)
    finally:
        await asyncio.to_thread(cost_pricesheet_service._safe_unlink, pricesheet_path)

    attrs["state-version"] = sv_meta
    logger.info(
        "workspace_cost_estimated",
        workspace_id=str(ws.id),
        state_version_id=str(sv.id),
        currency=attrs.get("currency"),
        monthly_max=round(attrs.get("total", {}).get("max", 0.0), 2),
        priced=len(attrs.get("resources", [])),
        unpriced=len(attrs.get("unpriced", [])),
    )
    return attrs
