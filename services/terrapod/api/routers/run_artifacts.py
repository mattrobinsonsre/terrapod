"""Authenticated artifact download/upload endpoints for runner Jobs.

Runners authenticate with a short-lived runner token (HMAC-signed, scoped
to a single run). The token's run_id must match the path run_id.

Downloads return 302 redirects to presigned storage URLs.
Uploads accept raw bytes and write to storage directly.

Endpoints:
    GET  /api/v2/runs/{run_id}/artifacts/config      — download config archive
    GET  /api/v2/runs/{run_id}/artifacts/state        — download current state
    GET  /api/v2/runs/{run_id}/artifacts/plan-file    — download plan file
    PUT  /api/v2/runs/{run_id}/artifacts/plan-log     — upload plan log
    PUT  /api/v2/runs/{run_id}/artifacts/plan-file    — upload plan file
    PUT  /api/v2/runs/{run_id}/artifacts/apply-log    — upload apply log
    PUT  /api/v2/runs/{run_id}/artifacts/state        — upload new state
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import Run, StateVersion
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.storage import get_storage
from terrapod.storage.keys import (
    apply_log_key,
    config_version_key,
    plan_log_key,
    plan_output_key,
    state_key,
)

router = APIRouter(prefix="/api/v2", tags=["run-artifacts"])
logger = get_logger(__name__)


def _require_runner_for_run(user: AuthenticatedUser, run_id: str) -> None:
    """Verify the user is a runner token scoped to this run."""
    if user.auth_method != "runner_token":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Runner token required",
        )
    if user.run_id != run_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token not scoped to this run",
        )


async def _get_run(run_id: str, db: AsyncSession) -> Run:
    """Get a run by UUID string."""
    run = await db.get(Run, uuid.UUID(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


# ── Downloads (302 redirect to presigned GET URL) ────────────────────────


@router.get("/runs/{run_id}/artifacts/config")
async def download_config(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Download the configuration archive for a run."""
    _require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    if not run.configuration_version_id:
        raise HTTPException(status_code=404, detail="No configuration version")

    storage = get_storage()
    key = config_version_key(str(run.workspace_id), str(run.configuration_version_id))
    url = await storage.presigned_get_url(key)
    return RedirectResponse(url=url.url, status_code=302)


@router.get("/runs/{run_id}/artifacts/state")
async def download_state(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Download the current state for the run's workspace."""
    _require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    result = await db.execute(
        select(StateVersion)
        .where(StateVersion.workspace_id == run.workspace_id)
        .order_by(StateVersion.serial.desc())
        .limit(1)
    )
    sv = result.scalar_one_or_none()
    if sv is None:
        raise HTTPException(status_code=404, detail="No state version")

    storage = get_storage()
    key = state_key(str(run.workspace_id), str(sv.id))
    url = await storage.presigned_get_url(key)
    return RedirectResponse(url=url.url, status_code=302)


@router.get("/runs/{run_id}/artifacts/plan-file")
async def download_plan_file(
    run_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Download the plan file from the plan phase."""
    _require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    storage = get_storage()
    key = plan_output_key(str(run.workspace_id), str(run.id))
    url = await storage.presigned_get_url(key)
    return RedirectResponse(url=url.url, status_code=302)


# ── Uploads (receive body, write to storage) ─────────────────────────────


@router.put("/runs/{run_id}/artifacts/plan-log")
async def upload_plan_log(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the plan log."""
    _require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = plan_log_key(str(run.workspace_id), str(run.id))
    await storage.put(key, body)
    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/plan-file")
async def upload_plan_file(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the plan file."""
    _require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = plan_output_key(str(run.workspace_id), str(run.id))
    await storage.put(key, body)
    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/apply-log")
async def upload_apply_log(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload the apply log."""
    _require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = apply_log_key(str(run.workspace_id), str(run.id))
    await storage.put(key, body)
    return Response(status_code=204)


@router.put("/runs/{run_id}/artifacts/state")
async def upload_state(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Upload new state after apply."""
    _require_runner_for_run(user, run_id)
    run = await _get_run(run_id, db)

    body = await request.body()
    storage = get_storage()
    key = state_key(str(run.workspace_id), f"{run.id}-new")
    await storage.put(key, body)
    return Response(status_code=204)
