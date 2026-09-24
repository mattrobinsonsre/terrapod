"""AI policy gate run endpoints (#1766).

The judgement-call sibling of `security_scanning.py` and `policy_sets.py`, and
deliberately the same shape: read the run's verdict, and let a workspace admin
override one that is holding a run.

There is **no runner protocol here**, and that is the one structural difference
from the other two gates. Their evidence is produced by the runner and POSTed
before plan-result; this verdict comes from the summariser, which runs in the
API, so there is nothing for a runner to fetch or report.

UX CONTRACT: consumed by the run-detail AI policy panel. Response shapes /
attribute names / status codes here MUST be matched by that page.

Endpoints (all under the native prefix):
    GET  /runs/{run_id}/ai-policy                    read the run's verdict
    POST /runs/{run_id}/actions/override-ai-policy   admin override (workspace admin)
"""

import uuid
from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db
from terrapod.engines import evaluates_ai_policy
from terrapod.logging_config import get_logger
from terrapod.services import ai_policy_service, run_service
from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for

router = APIRouter(tags=["ai-policy"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _evaluation_json(row) -> dict:
    """JSON:API attributes for an AIPolicyEvaluation (kebab-case)."""
    return {
        "id": f"aipol-{row.id}",
        "type": "ai-policy-evaluations",
        "attributes": {
            "enforcement-level": row.enforcement_level,
            "risk-threshold": row.risk_threshold,
            "outcome": row.outcome,
            "verdict": row.verdict or {},
            "risk-level": row.risk_level,
            "error": row.error,
            "overridden-by": row.overridden_by,
            "overridden-at": _rfc3339(row.overridden_at),
            "created-at": _rfc3339(row.created_at),
        },
    }


def _run_uuid(run_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(run_id.removeprefix("run-"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc


def _not_evaluated_reason(run: Run, ws: Workspace | None) -> str | None:
    """Why there is no verdict, rather than an unexplained null.

    A run with no row is ambiguous on its own — the gate may be off, the engine
    may not be ruled on, or the verdict may simply not have landed yet — and
    "waiting" versus "never coming" is exactly the distinction an operator
    staring at a held run needs.
    """
    if not evaluates_ai_policy(getattr(run, "engine", None) or getattr(ws, "engine", None)):
        return ai_policy_service.NOT_EVALUATED_ENGINE
    if run.plan_only:
        return "Speculative runs are not gated — there is no apply to block."
    if ai_policy_service.effective_enforcement(ws) == "off":
        return "The AI policy gate is not enabled for this workspace."
    if not ai_policy_service.is_configured():
        return (
            "The AI policy gate is enabled but has no deny criteria and no risk "
            "threshold, so there is nothing to rule against."
        )
    return "Waiting for the plan summary to produce a verdict."


@router.get("/runs/{run_id}/ai-policy")
async def get_run_ai_policy(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Read the AI policy verdict recorded for a run (one per run)."""
    run = await db.get(Run, _run_uuid(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.RUN_READ):
        raise HTTPException(status_code=403, detail="Requires read permission on workspace")

    row = await ai_policy_service.get_evaluation(db, run.id)
    meta: dict = {
        "enforcement-level": ai_policy_service.effective_enforcement(ws),
        "blocking": await ai_policy_service.run_is_ai_policy_blocked(db, run.id),
    }
    if row is None:
        reason = _not_evaluated_reason(run, ws)
        if reason:
            meta["not-evaluated-reason"] = reason
    return JSONResponse(
        content={
            "data": _evaluation_json(row) if row is not None else None,
            "meta": meta,
        }
    )


@router.post("/runs/{run_id}/actions/override-ai-policy")
async def override_run_ai_policy(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Override a run's blocking AI policy verdict. Requires workspace admin.

    After overriding, a run still held in `planning` is re-driven immediately
    rather than waiting for the next reconciler tick — the same contract as the
    policy and security-scan overrides.
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

    # Releases the hold even when no verdict was ever recorded. That used to be
    # a 409 on the grounds that there was nothing to override -- but a run held
    # BECAUSE no verdict landed is exactly the run an operator most needs to
    # release, and the advice to wait was advice to wait for something that was
    # never coming. The service writes an honest row for that case rather than
    # a forged pass.
    row = await ai_policy_service.override(
        db,
        run_id=run.id,
        actor=user.email,
        enforcement_level=ai_policy_service.effective_enforcement(ws),
    )
    await db.commit()

    if run.status == "planning":
        run = await run_service.complete_plan(db, run)
        await db.commit()

    logger.info(
        "AI policy verdict overridden",
        run_id=str(run.id),
        by=user.email,
        outcome=row.outcome,
    )
    return JSONResponse(
        content={
            "data": _evaluation_json(row),
            "meta": {"run-status": run.status},
        }
    )
