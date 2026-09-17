"""Policy checks — the TFE V2 surface the `tofu`/`terraform` CLI reads (#1704).

The CLI's cloud backend reads a run's policy checks after the plan, prints each
one's output, and on a `soft_failed` check asks whether to override it; with
`-auto-approve` it overrides the check itself when `permissions.can-override`
allows. See `services/policy_check_service.py` for how Terrapod's OPA and
security-scan gates map onto checks.

Endpoints (under /api/tfe/v2, and the /api/v2 alias):
    GET  /runs/{run_id}/policy-checks              list a run's checks
    GET  /policy-checks/{id}                       read one check
    GET  /policy-checks/{id}/output                the check's output, as text
    POST /policy-checks/{id}/actions/override      override a soft-failed check

The run advertises its checks in the `policy-checks` relationship only when the
response uses the Terraform Enterprise vocabulary (see `post_plan_decisions`),
because that is what makes the CLI act on them. These endpoints are served
either way.
"""

from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.ids import parse_id
from terrapod.api.pagination import paginate
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import policy_check_service, run_service
from terrapod.services.policy_check_service import PolicyCheck
from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for

router = APIRouter(tags=["policy-checks"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_json(check: PolicyCheck, *, can_override: bool) -> dict:
    timestamps = {"queued-at": _rfc3339(check.queued_at)}
    if check.status == policy_check_service.PASSED:
        timestamps["passed-at"] = _rfc3339(check.queued_at)
    elif check.status == policy_check_service.SOFT_FAILED:
        timestamps["soft-failed-at"] = _rfc3339(check.queued_at)
    elif check.decided_at is not None:
        timestamps["overridden-at"] = _rfc3339(check.decided_at)
    return {
        "id": check.id,
        "type": "policy-checks",
        "attributes": {
            "status": check.status,
            "scope": check.scope,
            "result": {
                "result": check.status != policy_check_service.SOFT_FAILED,
                "passed": check.passed,
                "total-failed": check.advisory_failed + check.soft_failed,
                "hard-failed": 0,
                "soft-failed": check.soft_failed,
                "advisory-failed": check.advisory_failed,
                "duration": 0,
            },
            "actions": {"is-overridable": check.is_overridable},
            "permissions": {"can-override": can_override},
            "status-timestamps": {k: v for k, v in timestamps.items() if v is not None},
        },
        "relationships": {
            "run": {"data": {"id": f"run-{check.run_id}", "type": "runs"}},
        },
        "links": {"output": f"/api/tfe/v2/policy-checks/{check.id}/output"},
    }


async def _run_and_caps(db: AsyncSession, user: AuthenticatedUser, run_uuid) -> tuple[Run, set]:
    run = await db.get(Run, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Run not found")
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.RUN_READ):
        # Same answer as a run that does not exist: a caller who cannot read
        # the workspace learns nothing about its runs.
        raise HTTPException(status_code=404, detail="Run not found")
    return run, caps


async def _check_for(
    db: AsyncSession, user: AuthenticatedUser, value: str
) -> tuple[Run, set, PolicyCheck]:
    parsed = policy_check_service.parse_check_id(value)
    if parsed is None:
        raise HTTPException(status_code=404, detail="Policy check not found")
    kind, run_uuid = parsed
    try:
        run, caps = await _run_and_caps(db, user, run_uuid)
    except HTTPException as exc:
        raise HTTPException(status_code=404, detail="Policy check not found") from exc
    check = await policy_check_service.get_check(db, run, kind)
    if check is None:
        raise HTTPException(status_code=404, detail="Policy check not found")
    return run, caps, check


def _can_override(caps: set) -> bool:
    # The same capability the Policy Checks and Security panels' overrides need.
    return has_capability(caps, cap.WORKSPACE_SETTINGS)


@router.get("/runs/{run_id}/policy-checks")
async def list_policy_checks(
    request: Request,
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """A run's policy checks: OPA first, then the security scan."""
    run_uuid = parse_id(run_id, "run-", detail="Run not found")
    run, caps = await _run_and_caps(db, user, run_uuid)
    checks = await policy_check_service.list_checks(db, run)
    can_override = _can_override(caps)
    data = [_check_json(c, can_override=can_override) for c in checks]
    page, meta = paginate(data, request)
    return JSONResponse(content={"data": page, "meta": meta})


@router.get("/policy-checks/{check_id}")
async def show_policy_check(
    check_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    _, caps, check = await _check_for(db, user, check_id)
    return JSONResponse(content={"data": _check_json(check, can_override=_can_override(caps))})


@router.get("/policy-checks/{check_id}/output")
async def policy_check_output(
    check_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PlainTextResponse:
    """What the check found, as the CLI prints it."""
    _, _, check = await _check_for(db, user, check_id)
    return PlainTextResponse(check.output + "\n")


@router.post("/policy-checks/{check_id}/actions/override")
async def override_policy_check(
    check_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Override a soft-failed check, then move the run on at once.

    Re-driving here rather than at the next reconciler tick matters to the CLI:
    straight after overriding it reads the run and applies only if the run is
    confirmable by then.
    """
    run, caps, check = await _check_for(db, user, check_id)
    if not _can_override(caps):
        raise HTTPException(status_code=403, detail="Requires admin permission on workspace")
    if not check.is_overridable:
        raise HTTPException(
            status_code=409,
            detail=f"Policy check is {check.status}; only a soft_failed check can be overridden",
        )

    count = await policy_check_service.override_check(db, run, check.kind, user.email)
    await db.commit()
    if run.status == "planning":
        run = await run_service.complete_plan(db, run)
        await db.commit()

    logger.info(
        "Policy check overridden",
        run_id=str(run.id),
        check=check.kind,
        overridden=count,
        by=user.email,
    )
    check = await policy_check_service.get_check(db, run, check.kind)
    return JSONResponse(content={"data": _check_json(check, can_override=True)})
