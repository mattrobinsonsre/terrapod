"""Security-scan run endpoints — Checkov/Trivy IaC misconfig scanning (#1036).

The deterministic security-scan stage, the structural twin of the OPA policy
endpoints (``policy_sets.py``). Per-workspace config lives on the workspace
(managed via the workspace PATCH); this router exposes the run-lifecycle surface:
the runner pulls its scan config and posts results, operators read the result,
and a workspace admin can override a blocking scan.

UX CONTRACT: consumed by the run-detail Security panel. Response shapes /
attribute names / status codes here MUST be matched by that page.

Endpoints (all under /api/terrapod/v1):
    Run lifecycle (workspace read/admin):
        GET  /runs/{run_id}/security-scan                     read the run's scan result
        POST /runs/{run_id}/actions/override-security-scan    admin override (workspace admin)
    Runner protocol (runner token, run_id-scoped):
        GET  /runs/{run_id}/security-scan-config              engine/enforcement/threshold/skip
        POST /runs/{run_id}/security-scan-results             record the scan outcome
"""

import uuid
from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import (
    AuthenticatedUser,
    get_current_user,
    require_runner_for_run,
)
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import run_service, security_scan_service
from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for

router = APIRouter(tags=["security-scanning"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scan_json(scan) -> dict:
    """JSON:API attributes for a SecurityScanResult (kebab-case)."""
    return {
        "id": f"ss-{scan.id}",
        "type": "security-scan-results",
        "attributes": {
            "engine": scan.engine,
            "enforcement-level": scan.enforcement_level,
            "severity-threshold": scan.severity_threshold,
            "outcome": scan.outcome,
            "findings": scan.findings or [],
            "summary": scan.summary or {},
            "error": scan.error,
            "overridden-by": scan.overridden_by,
            "overridden-at": _rfc3339(scan.overridden_at),
            "created-at": _rfc3339(scan.created_at),
        },
    }


async def _get_run_for_read(db: AsyncSession, run_id: str, user: AuthenticatedUser) -> Run:
    try:
        run_uuid = uuid.UUID(run_id.removeprefix("run-"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    run = await db.get(Run, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.RUN_READ):
        raise HTTPException(status_code=403, detail="Requires read permission on workspace")
    return run


def _run_uuid(run_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(run_id.removeprefix("run-"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc


# ── Run lifecycle (workspace read/admin) ──────────────────────────────────


@router.get("/runs/{run_id}/security-scan")
async def get_run_security_scan(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Read the security-scan result recorded for a run (one per run)."""
    run = await _get_run_for_read(db, run_id, user)
    scan = await security_scan_service.get_run_scan(db, run.id)
    summary = await security_scan_service.run_scan_summary(db, run.id)
    meta: dict = {"summary": summary}
    return JSONResponse(
        content={
            "data": _scan_json(scan) if scan is not None else None,
            "meta": meta,
        }
    )


@router.post("/runs/{run_id}/actions/override-security-scan")
async def override_run_security_scan(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Override a run's blocking security scan. Requires workspace admin.

    After overriding, a run still held in ``planning`` by the scan gate is
    re-driven immediately (mirrors the policy override), rather than waiting for
    the next reconciler tick.
    """
    run = await db.get(Run, _run_uuid(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.WORKSPACE_SETTINGS):
        raise HTTPException(status_code=403, detail="Requires admin permission on workspace")

    count = await security_scan_service.override_run_scan(db, run.id, user.email)
    await db.commit()

    if run.status == "planning":
        run = await run_service.complete_plan(db, run)
        await db.commit()

    logger.info(
        "Security scan overridden",
        run_id=str(run.id),
        overridden=count,
        by=user.email,
    )
    scan = await security_scan_service.get_run_scan(db, run.id)
    return JSONResponse(
        content={
            "data": _scan_json(scan) if scan is not None else None,
            "meta": {"overridden": count, "run-status": run.status},
        }
    )


# ── Runner protocol (runner-token auth, run_id-scoped) ─────────────────────


@router.get("/runs/{run_id}/security-scan-config")
async def get_security_scan_config(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """The per-workspace scan config for this run — flat JSON (runner only).

    ``enabled: false`` (enforcement ``off``) tells the runner to skip the stage.
    """
    require_runner_for_run(user, run_id)
    run = await db.get(Run, _run_uuid(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return JSONResponse(content=security_scan_service.resolve_scan_config(ws))


@router.post("/runs/{run_id}/security-scan-results", status_code=201)
async def post_security_scan_results(
    run_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Record the runner's scan result for this run (idempotent, ON CONFLICT).

    Body: ``{engine, outcome, findings, summary, error}``. The **enforcement
    level and severity threshold are re-resolved server-side from the workspace**
    (not trusted from the runner) so a buggy or compromised runner can't post
    ``advisory`` to slip past a ``enforced`` gate.
    """
    require_runner_for_run(user, run_id)
    run = await db.get(Run, _run_uuid(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    engine = str(body.get("engine") or "")
    outcome = str(body.get("outcome") or "")
    if outcome not in security_scan_service.VALID_OUTCOME:
        raise HTTPException(status_code=422, detail=f"invalid outcome: {outcome!r}")
    if engine and engine not in security_scan_service.VALID_ENGINE:
        raise HTTPException(status_code=422, detail=f"invalid engine: {engine!r}")
    findings = body.get("findings")
    if not isinstance(findings, list):
        raise HTTPException(status_code=422, detail="findings must be a list")
    summary = body.get("summary")
    if summary is not None and not isinstance(summary, dict):
        raise HTTPException(status_code=422, detail="summary must be an object")

    # Authoritative enforcement/threshold from the workspace, NOT the runner.
    enforcement = ws.security_scan_enforcement or "off"
    threshold = ws.security_scan_severity_threshold or "high"

    await security_scan_service.record_scan_result(
        db,
        run_id=run.id,
        engine=engine,
        enforcement_level=enforcement,
        severity_threshold=threshold,
        outcome=outcome,
        findings=findings,
        summary=summary or {},
        error=(str(body["error"]) if body.get("error") else None),
    )
    await db.commit()
    return JSONResponse(content={"recorded": 1}, status_code=201)
