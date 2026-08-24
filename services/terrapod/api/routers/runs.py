"""Run CRUD and lifecycle endpoints (TFE V2 compatible).

UX CONTRACT: Run endpoints are consumed by the web frontend:
  - web/src/app/workspaces/[id]/page.tsx (runs tab: list, create)
  - web/src/app/workspaces/[id]/runs/[runId]/page.tsx (run detail, logs, confirm/discard/cancel)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to those frontend pages.

Endpoints:
    POST   /api/v2/runs                              (create run)
    GET    /api/v2/runs/{run_id}                      (show run)
    GET    /api/v2/workspaces/{id}/runs               (list runs)
    POST   /api/v2/runs/{run_id}/actions/apply        (confirm plan for apply)
    POST   /api/v2/runs/{run_id}/actions/discard      (discard plan)
    POST   /api/v2/runs/{run_id}/actions/cancel       (cancel run)
    POST   /api/terrapod/v1/runs/{run_id}/actions/retry        (retry run — create new run from terminal run)
    GET    /api/terrapod/v1/runs/{run_id}/plan                 (plan details)
    GET    /api/v2/plans/{plan_id}                    (plan details by ID)
    GET    /api/v2/plans/{plan_id}/log                (plan log stream)
    GET    /api/v2/plans/{plan_id}/json-output        (structured JSON plan; 302 → presigned)
    GET    /api/terrapod/v1/runs/{run_id}/apply                (apply details)
    GET    /api/v2/applies/{apply_id}                 (apply details by ID)
    PATCH  /api/terrapod/v1/listeners/{id}/runs/{run_id}       (listener status update)
    GET    /api/terrapod/v1/listeners/{id}/runs/next            (poll for next run)
"""

import asyncio
import json
import re
import uuid
from datetime import UTC
from typing import Literal

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from terrapod.api.dependencies import (
    AuthenticatedUser,
    ListenerIdentity,
    get_current_user,
    get_listener_identity,
)
from terrapod.api.errors import vcs_unavailable
from terrapod.api.pagination import build_meta
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.config import settings
from terrapod.db.models import (
    ConfigurationVersion,
    CostSummary,
    CostSummaryMessage,
    PlanSummary,
    PlanSummaryMessage,
    Run,
    StateVersion,
    VCSConnection,
    Workspace,
    now_utc,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import agent_pool_service, plan_graph_service, pool_set, run_service
from terrapod.services.workspace_rbac_service import (
    resolve_workspace_capabilities_for,
)
from terrapod.storage import get_storage
from terrapod.storage.keys import (
    apply_log_key,
    cost_estimate_key,
    plan_json_output_key,
    plan_log_key,
)
from terrapod.storage.protocol import ObjectNotFoundError

router = APIRouter(prefix="/api/v2", tags=["runs"])

# Terrapod-only run endpoints — listener protocol (claim/launch/status/log
# streaming), runner-driven completion (plan-result, apply-result), SSE
# event channels, and the retry action. Dual-mounted under
# /api/terrapod/v1 (canonical) and /api/v2 (deprecated, removed in v0.24.0
# — see #278).
extensions_router = APIRouter(tags=["run-extensions"])
logger = get_logger(__name__)

# States where cancellation is a sensible action — the run is actively
# moving through the pipeline. `planned` (awaiting user confirm/discard)
# is deliberately excluded: the right actions there are confirm/discard,
# not cancel. Terminal states (applied/errored/discarded/canceled) aren't
# here because there's nothing left running.
_CANCELABLE_STATES = frozenset({"pending", "queued", "planning", "confirmed", "applying"})


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plan_summary_attr(run: Run) -> dict[str, int] | None:
    """Compact plan summary used by the UI to render the badge row.

    Returns None when the runner hasn't uploaded a JSON plan yet, or the
    upload was unparseable — the UI uses None to mean "we don't know"
    and renders nothing, vs an all-zero dict which means "no changes".
    """
    if run.resource_additions is None:
        return None
    return {
        "add": run.resource_additions or 0,
        "change": run.resource_changes or 0,
        "destroy": run.resource_destructions or 0,
        "replace": run.resource_replacements or 0,
        "import": run.resource_imports or 0,
    }


def _run_json(
    run: Run,
    *,
    workspace_name: str = "",
    workspace_has_vcs: bool = False,
    state_version_id: str | None = None,
) -> dict:
    """Serialize a Run to TFE V2 JSON:API format."""
    run_id = f"run-{run.id}"

    return {
        "data": {
            "id": run_id,
            "type": "runs",
            "attributes": {
                "status": run.status,
                "message": run.message,
                # Why a run was discarded (state changed / plan expired /
                # superseded), null otherwise (#646/#647).
                "discard-reason": run.discard_reason,
                "is-destroy": run.is_destroy,
                "auto-apply": run.auto_apply,
                # Conditional auto-apply (#1274): the mode this run was
                # created under, and — when it declined — why, so the UI can
                # explain a run sitting in `planned` rather than leaving the
                # operator to infer it from the counts.
                "auto-apply-mode": run.auto_apply_mode,
                "auto-apply-declined-reason": run.auto_apply_declined_reason,
                "plan-only": run.plan_only,
                "source": run.source,
                "execution-backend": run.execution_backend,
                "terraform-version": run.terraform_version,
                "terragrunt-enabled": run.terragrunt_enabled,
                "terragrunt-version": run.terragrunt_version,
                "resource-cpu": run.resource_cpu,
                "parallelism": run.parallelism,
                "resource-memory": run.resource_memory,
                # Which agent pool has this run (#1231). At creation this is
                # element 0 of the workspace's pool set; on claim it is
                # REWRITTEN to the pool that actually took it. So on a finished
                # run this answers "where did this execute?" — which with
                # multi-pool routing (#1085) is the first thing you want to know
                # and was previously only visible in the database.
                #
                # Note this differs from the same-named attribute on a
                # workspace, where it is always a projection of element 0. On a
                # run it is the claimant. Null on local-execution runs, and on
                # agent runs whose pool has since been deleted.
                "agent-pool-id": (f"apool-{run.pool_id}" if run.pool_id else None),
                # Every pool that COULD have claimed it, snapshotted at run
                # creation and preserved (only re-ordered) across the claim.
                # Deliberately named apart from the workspace's
                # `agent-pool-ids`, because that is the live configured set
                # and this is a point-in-time snapshot that does not move when
                # the workspace is later re-pointed. Together with the
                # attribute above: "these were the candidates, that one took
                # it."
                "candidate-agent-pool-ids": [f"apool-{p}" for p in pool_set.run_pool_ids(run)],
                # Runner resource profile + OOM detection (#430). All
                # nullable / empty-string for runs that pre-date the
                # feature (or where the runner died before capturing).
                "peak-memory-bytes": run.peak_memory_bytes,
                "peak-cpu-usec": run.peak_cpu_usec,
                "runner-exit-code": run.runner_exit_code,
                "runner-exit-reason": run.runner_exit_reason or "",
                "runner-exit-status": run.runner_exit_status or "",
                "error-message": run.error_message,
                "target-addrs": run.target_addrs or [],
                "replace-addrs": run.replace_addrs or [],
                "refresh-only": run.refresh_only,
                "refresh": run.refresh,
                "allow-empty-apply": run.allow_empty_apply,
                "is-drift-detection": run.is_drift_detection,
                "has-changes": run.has_changes,
                # Whether a structured JSON plan exists — gates the Impact
                # graph tab in the UI (#761).
                "has-json-output": bool(run.has_json_output),
                # Cost estimation (#871): whether a cost estimate artifact
                # exists (gates the Cost tab + download URL) and the cached
                # monthly total range for cheap list display.
                "has-cost-estimate": bool(run.has_cost_estimate),
                "cost-currency": run.cost_currency,
                "cost-monthly-min": run.cost_monthly_min,
                "cost-monthly-max": run.cost_monthly_max,
                "plan-summary": _plan_summary_attr(run),
                "workspace-name": workspace_name,
                "workspace-has-vcs": workspace_has_vcs,
                "module-overrides": run.module_overrides,
                "vcs-commit-sha": run.vcs_commit_sha,
                "vcs-branch": run.vcs_branch,
                "vcs-pull-request-number": run.vcs_pull_request_number,
                "status-timestamps": {
                    k: v
                    for k, v in {
                        "plan-queued-at": _rfc3339(run.created_at),
                        "planning-at": _rfc3339(run.plan_started_at),
                        "planned-at": _rfc3339(run.plan_finished_at),
                        "applying-at": _rfc3339(run.apply_started_at),
                        "applied-at": _rfc3339(run.apply_finished_at),
                    }.items()
                    if v
                },
                "created-at": _rfc3339(run.created_at),
                "updated-at": _rfc3339(run.updated_at),
                "created-by": run.created_by or "",
                # Confirm/Discard are only meaningful for planned runs that
                # actually have work to apply. A plan with has_changes=False
                # is a no-op — the reconciler short-circuits it straight to
                # `applied`, so seeing `planned` + has_changes=False here is
                # only possible for legacy runs created before that fix.
                # Hide the buttons so the UI doesn't push users into the
                # state-upload 500 path.
                #
                # The auto-apply test is on the resolved MODE, not the boolean
                # column. `create_run` sets `run.auto_apply = True` for every
                # non-`never` mode, so keying off the boolean hid Confirm on a
                # conditional run that had been *held* — the run sat in
                # `planned` with a declined-reason and no consumer offered the
                # one action that resolves it, while `confirm_run` (which only
                # checks `status == "planned"`) would have accepted it. Only
                # `always` genuinely needs the button hidden: such a run
                # applies itself and never waits for a human.
                "actions": {
                    "is-confirmable": run.status == "planned"
                    and run_service.resolve_auto_apply_mode(run) != "always"
                    and not run.plan_only
                    and run.has_changes is not False,
                    "is-discardable": run.status == "planned"
                    and not run.plan_only
                    and run.has_changes is not False,
                    # Cancel only applies to in-progress states. In particular
                    # `planned` (awaiting confirmation) is NOT cancelable —
                    # the user should confirm or discard. Terminal states
                    # aren't cancelable either (nothing running).
                    "is-cancelable": run.status in _CANCELABLE_STATES,
                    "is-retryable": run.status in run_service.TERMINAL_STATES
                    or (run.plan_only and run.status == "planned"),
                },
                "permissions": {
                    "can-apply": run.status == "planned"
                    and not run.plan_only
                    and run.has_changes is not False,
                    "can-cancel": run.status in _CANCELABLE_STATES,
                    "can-discard": run.status == "planned"
                    and not run.plan_only
                    and run.has_changes is not False,
                    "can-retry": run.status in run_service.TERMINAL_STATES
                    or (run.plan_only and run.status == "planned"),
                    "can-force-execute": False,
                    "can-force-cancel": False,
                },
            },
            "relationships": {
                "workspace": {
                    "data": {"id": f"ws-{run.workspace_id}", "type": "workspaces"},
                },
                # The canonical link form for the pool that has the run; the
                # `agent-pool-id` attribute above is kept alongside it (#1063).
                "agent-pool": {
                    "data": (
                        {"id": f"apool-{run.pool_id}", "type": "agent-pools"}
                        if run.pool_id
                        else None
                    ),
                },
                "configuration-version": {
                    "data": (
                        {
                            "id": f"cv-{run.configuration_version_id}",
                            "type": "configuration-versions",
                        }
                        if run.configuration_version_id
                        else None
                    ),
                },
                "plan": {
                    "data": {"id": f"plan-{run.id}", "type": "plans"},
                },
                "apply": {
                    "data": {"id": f"apply-{run.id}", "type": "applies"},
                },
                "task-stages": {
                    "links": {"related": f"/api/terrapod/v1/runs/{run_id}/task-stages"},
                },
                "policy-checks": {
                    "links": {"related": f"/api/terrapod/v1/runs/{run_id}/policy-evaluations"},
                },
                "created-state-version": {
                    "data": (
                        {"id": state_version_id, "type": "state-versions"}
                        if state_version_id
                        else None
                    ),
                },
            },
            "links": {
                "self": f"/api/v2/runs/{run_id}",
            },
        }
    }


async def _get_run(run_id: str, db: AsyncSession) -> Run:
    run_uuid = uuid.UUID(run_id.removeprefix("run-"))
    run = await run_service.get_run(db, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


async def _get_workspace(workspace_id: str, db: AsyncSession) -> Workspace:
    ws_uuid = workspace_id.removeprefix("ws-")
    result = await db.execute(select(Workspace).where(Workspace.id == ws_uuid))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


async def _require_run_ws_capability(
    run: Run, required: str, user: AuthenticatedUser, db: AsyncSession
) -> None:
    """Check that user holds the required capability on the run's workspace."""
    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, required):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires '{required}' capability on workspace",
        )


async def _fetch_vcs_config(
    db: AsyncSession, ws: Workspace, *, ref_override: str = ""
) -> tuple[uuid.UUID, str, str]:
    """HTTP wrapper over `vcs_config_service.fetch_config_version`.

    The fetch itself moved to a service (#1396) so the run-trigger path could
    share it; this keeps the HTTP mapping where it belongs. A workspace we
    cannot fetch for is a 422; a provider outage is somebody else's 5xx, not
    ours, and gets 502/504 with the marker header (#1358).
    """
    from terrapod.services import vcs_config_service

    conn = await db.get(VCSConnection, ws.vcs_connection_id)
    try:
        return await vcs_config_service.fetch_config_version(db, ws, ref_override=ref_override)
    except vcs_config_service.VCSConfigError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except httpx.HTTPError as e:
        repo = ws.vcs_repo_url or ""
        raise vcs_unavailable(conn, repo, ref_override or ws.vcs_branch, e) from e


@router.post("/runs", status_code=201)
async def create_run(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a new run. Plan-only requires plan; apply requires write."""
    attrs = body.get("data", {}).get("attributes", {})
    relationships = body.get("data", {}).get("relationships", {})

    ws_data = relationships.get("workspace", {}).get("data", {})
    ws_id = ws_data.get("id", "")
    if not ws_id:
        raise HTTPException(status_code=422, detail="Workspace relationship is required")

    ws = await _get_workspace(ws_id, db)

    # CLI-initiated runs on VCS-connected agent workspaces: plan is allowed,
    # apply is not — VCS is the source of truth. Non-VCS ("CLI-driven") agent
    # workspaces allow both plan and apply from the CLI.
    # The guard fires when a configuration version is provided (CLI upload).
    # Runs without a CV (UI-queued) will fetch code from VCS downstream.
    plan_only = attrs.get("plan-only", False)
    cv_data = relationships.get("configuration-version", {}).get("data", {})
    cv_id_raw = cv_data.get("id", "") if cv_data else ""
    has_cv = bool(cv_id_raw)
    # Parse the CV id ONCE, up front, with a guard: a malformed id is a client
    # error (422), not a 500. Reused below for the speculative check and the
    # run's configuration_version_id.
    cv_uuid: uuid.UUID | None = None
    if has_cv:
        try:
            cv_uuid = uuid.UUID(cv_id_raw.removeprefix("cv-"))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid configuration-version id",
            ) from exc
    # A run against a speculative configuration version is ALWAYS plan-only
    # (TFE/HCP parity). The cloud backend uploads a speculative CV for
    # `tofu plan` and relies on the server to infer plan-only rather than
    # always setting the run's `plan-only` attribute. Without honoring the CV
    # flag, a CLI `plan` on a VCS-connected workspace is mis-read as an apply
    # and rejected by the guard below (#661).
    if cv_uuid is not None:
        from terrapod.db.models import ConfigurationVersion

        _spec_cv = await db.get(ConfigurationVersion, cv_uuid)
        if _spec_cv is not None and _spec_cv.speculative:
            plan_only = True
    # Config-managed guardrail (#535): a catalog-managed workspace runs only the
    # wrapper config the catalog generated for it. A run that pins a different
    # configuration version would diverge it from its catalog item — reject.
    # CV-less re-runs (re-plan/re-apply of the generated config) stay allowed.
    if ws.catalog_item_id is not None and has_cv:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This workspace is managed by the service catalog; runs cannot pin a "
                "custom configuration version. Manage it through the catalog."
            ),
        )
    if ws.execution_mode == "agent" and ws.vcs_connection_id is not None and has_cv:
        if not plan_only:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Apply is not allowed from the CLI on VCS-connected agent workspaces. "
                "Use 'tofu plan' for speculative plans, or trigger applies via VCS integration and/or the UI.",
            )
        plan_only = True

    # Check capability: plan-only needs run:plan; apply needs run:apply, or
    # run:apply-destroy when the run is a destroy (is-destroy=true).
    if plan_only:
        required_cap = cap.RUN_PLAN
    elif attrs.get("is-destroy", False):
        required_cap = cap.RUN_APPLY_DESTROY
    else:
        required_cap = cap.RUN_APPLY
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, required_cap):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires '{required_cap}' capability on workspace",
        )

    # Configuration version (optional) — cv_uuid was parsed + validated above
    # from the same relationship (422 on a malformed id).

    # VCS ref override: plan against an arbitrary branch/tag (always plan-only)
    vcs_ref = attrs.get("vcs-ref", "")
    if vcs_ref:
        if not ws.vcs_connection_id or not ws.vcs_repo_url:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="vcs-ref can only be used on VCS-connected workspaces",
            )
        plan_only = True  # Server-side enforcement — non-default refs are always plan-only

    # If no config version provided and workspace has VCS, fetch code from VCS
    vcs_sha = ""
    vcs_branch = ""
    if cv_uuid is None and ws.vcs_connection_id and ws.vcs_repo_url:
        cv_uuid, vcs_sha, vcs_branch = await _fetch_vcs_config(db, ws, ref_override=vcs_ref)

    # Non-VCS workspace, no CV in the request body (e.g. "Queue Plan" from
    # the UI) — fall back to the latest CLI-uploaded CV. Without this the
    # run is created with configuration_version_id=NULL and the runner
    # 404s trying to download the archive (#358). If no upload has ever
    # succeeded, fail loudly rather than create an unrunnable run.
    #
    # The `or not ws.vcs_repo_url` arm catches a misconfigured workspace
    # with a VCS connection but no repo URL: the VCS branch above
    # requires BOTH to be truthy so would have skipped it, leaving a
    # NULL CV. Treat it the same as a non-VCS workspace here — we can't
    # fetch code from a URL that doesn't exist either way.
    if cv_uuid is None and (not ws.vcs_connection_id or not ws.vcs_repo_url):
        latest_cv = await run_service.get_latest_uploaded_cv(db, ws.id)
        if latest_cv is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Workspace has no uploaded configuration. "
                "Upload one via 'tofu plan' / 'tofu apply' (CLI), or POST a "
                "configuration version + tarball before queueing a run.",
            )
        cv_uuid = latest_cv.id

    run = await run_service.create_run(
        db,
        workspace=ws,
        message=attrs.get("message", ""),
        is_destroy=attrs.get("is-destroy", False),
        auto_apply=attrs.get("auto-apply"),
        plan_only=plan_only,
        source="tfe-api",
        terraform_version=attrs.get("terraform-version", ""),
        configuration_version_id=cv_uuid,
        created_by=user.email,
        is_drift_detection=attrs.get("is-drift-detection", False),
        target_addrs=attrs.get("target-addrs"),
        replace_addrs=attrs.get("replace-addrs"),
        refresh_only=attrs.get("refresh-only", False),
        refresh=attrs.get("refresh", True),
        allow_empty_apply=attrs.get("allow-empty-apply", False),
    )

    # Attach VCS metadata if we fetched code from VCS
    if vcs_sha:
        run.vcs_commit_sha = vcs_sha
        run.vcs_branch = vcs_branch

    # Queue immediately if no config needed, or config already uploaded
    if cv_uuid is None:
        run = await run_service.queue_run(db, run)
    else:
        from terrapod.db.models import ConfigurationVersion

        cv = await db.get(ConfigurationVersion, cv_uuid)
        if cv and cv.status == "uploaded":
            run = await run_service.queue_run(db, run)

    await db.commit()
    await db.refresh(run)

    return JSONResponse(
        content=_run_json(
            run,
            workspace_name=ws.name,
            workspace_has_vcs=ws.vcs_connection_id is not None,
        ),
        status_code=201,
    )


@router.get("/runs/{run_id}")
async def show_run(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a run. Requires read on workspace."""
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)
    ws = await db.get(Workspace, run.workspace_id)

    # Look up state version created by this run (detail endpoint only)
    sv_result = await db.execute(
        select(StateVersion.id).where(StateVersion.run_id == run.id).limit(1)
    )
    sv_uuid = sv_result.scalar_one_or_none()
    sv_id = f"sv-{sv_uuid}" if sv_uuid else None

    return JSONResponse(
        content=_run_json(
            run,
            workspace_name=ws.name if ws else "",
            workspace_has_vcs=bool(ws and ws.vcs_connection_id),
            state_version_id=sv_id,
        ),
    )


@router.get("/workspaces/{workspace_id}/runs")
async def list_workspace_runs(
    workspace_id: str = Path(...),
    page_number: int = Query(1, alias="page[number]"),
    page_size: int = Query(20, alias="page[size]"),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List runs for a workspace. Requires read."""
    ws = await _get_workspace(workspace_id, db)
    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.RUN_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires read permission on workspace",
        )
    # Runs are the one deliberate exception to the "absent → full list"
    # convention: run history grows without bound, so this endpoint keeps a
    # sensible bounded default page (20) rather than returning everything.
    # It honours page[size]/page[number] and always emits meta.pagination so
    # clients can page through the full history. (See pagination.py.)
    if page_size < 1:
        page_size = 20
    page_size = min(page_size, 100)
    if page_number < 1:
        page_number = 1

    runs = await run_service.list_workspace_runs(db, ws.id, page_number, page_size)
    total = await run_service.count_workspace_runs(db, ws.id)
    has_vcs = ws.vcs_connection_id is not None
    return JSONResponse(
        content={
            "data": [
                _run_json(r, workspace_name=ws.name, workspace_has_vcs=has_vcs)["data"]
                for r in runs
            ],
            "meta": build_meta(total, page_number, page_size),
        }
    )


@router.post("/runs/{run_id}/actions/apply")
async def confirm_run(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Confirm a planned run for apply. Requires write."""
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(
        run, cap.RUN_APPLY_DESTROY if run.is_destroy else cap.RUN_APPLY, user, db
    )

    # No-op apply guard: a plan with has_changes=False has nothing to apply.
    # The reconciler short-circuits these directly to `applied`, so this
    # endpoint should only see them in narrow races (or for legacy data
    # predating that fix). Reject explicitly so the API and the UI surface
    # the same answer.
    if run.has_changes is False:
        raise HTTPException(
            status_code=422,
            detail="Plan reported no changes — there is nothing to apply.",
        )

    # Block apply of CLI-uploaded code on VCS-connected agent workspaces.
    #
    # The test is the CONFIGURATION VERSION's source, not the run's (#1307).
    # What this guard protects against is applying code that came in over the
    # API on a workspace whose code is supposed to come from VCS — that is a
    # property of the code, and the code is the CV. Keying it off the run's
    # source instead made it a proxy that was wrong in one direction: a run
    # trigger fired by an upstream apply created its run with `source="tfe-api"`
    # and pointed it at the destination's latest VCS-fetched CV, so it was
    # refused for not being VCS-managed while applying VCS-managed code. That
    # left the downstream half of run triggers non-functional on exactly the
    # workspaces most likely to use them.
    #
    # Exempt for the same reasons as before: destroys don't depend on uploaded
    # code at all, and a run already carrying a `vcs_commit_sha` was fetched
    # from VCS by the poller or a UI-queued ref.
    if not run.is_destroy and not run.vcs_commit_sha and run.source != "drift-detection":
        ws = await db.get(Workspace, run.workspace_id)
        if ws and ws.execution_mode == "agent" and ws.vcs_connection_id is not None:
            cv_source = ""
            if run.configuration_version_id:
                cv = await db.get(ConfigurationVersion, run.configuration_version_id)
                cv_source = (cv.source if cv else "") or ""
            if cv_source != "vcs":
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "This run's configuration version was uploaded over the API "
                        f"(source '{cv_source or 'unknown'}'), and this workspace is "
                        "VCS-connected — only VCS-managed code can be applied here. "
                        "Push the change and let the VCS poller create the run."
                    ),
                )

    try:
        run = await run_service.confirm_run(db, run)
        await db.commit()
    except run_service.ApplyBlocked as e:
        # Mergeability gate rejected (apply-then-merge mode). `vcs_apply_blocked_reason`
        # is already persisted on the run by the gate; the 422 surfaces the
        # provider's own language to the API caller (web UI, terraform CLI).
        await db.commit()
        raise HTTPException(status_code=422, detail=str(e.reason)) from e
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return JSONResponse(content=_run_json(run))


@router.post("/runs/{run_id}/actions/discard")
async def discard_run(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Discard a planned run. Requires plan."""
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_CANCEL, user, db)
    try:
        run = await run_service.discard_run(db, run)
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return JSONResponse(content=_run_json(run))


@router.post("/runs/{run_id}/actions/cancel")
async def cancel_run(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Cancel a run. Requires plan."""
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_CANCEL, user, db)
    try:
        run = await run_service.cancel_run(db, run)
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return JSONResponse(content=_run_json(run))


@extensions_router.post("/runs/{run_id}/actions/retry")
async def retry_run(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Retry a terminal run by creating a new run with the same parameters.

    Creates a new run for the same workspace using the same configuration
    version, VCS metadata, and settings as the original run. Only terminal
    runs (errored, canceled, discarded, applied, planned plan-only) can be retried.
    Requires plan permission.
    """
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_CANCEL, user, db)

    is_terminal = run.status in run_service.TERMINAL_STATES or (
        run.plan_only and run.status == "planned"
    )
    if not is_terminal:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot retry run in non-terminal state '{run.status}'",
        )

    ws = await db.get(Workspace, run.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Defensive: if the source run has no CV (e.g. it was queued by an
    # older fire_run_triggers before #439, or by some other path that
    # forgot to attach one), pick the workspace's latest uploaded CV
    # rather than faithfully copying the null forward. A retry with no
    # CV would just re-hit the runner's "no configuration archive (HTTP
    # 404)" exit — preserving the bug instead of clearing it. Existing
    # null-CV runs already in the DB become retry-able after this.
    cv_id_for_retry = run.configuration_version_id
    if cv_id_for_retry is None:
        from terrapod.db.models import ConfigurationVersion

        cv_result = await db.execute(
            select(ConfigurationVersion.id)
            .where(
                ConfigurationVersion.workspace_id == ws.id,
                ConfigurationVersion.status == "uploaded",
            )
            .order_by(ConfigurationVersion.created_at.desc())
            .limit(1)
        )
        cv_id_for_retry = cv_result.scalar_one_or_none()
        if cv_id_for_retry is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Source run has no configuration version and the workspace has "
                    "never had one uploaded; nothing to retry against."
                ),
            )

    new_run = await run_service.create_run(
        db,
        workspace=ws,
        message=f"Retry of run-{run.id}",
        source=run.source,
        plan_only=run.plan_only,
        configuration_version_id=cv_id_for_retry,
        created_by=user.email,
        target_addrs=run.target_addrs,
        replace_addrs=run.replace_addrs,
        refresh_only=run.refresh_only,
        refresh=run.refresh,
        allow_empty_apply=run.allow_empty_apply,
    )

    # Copy VCS metadata and module overrides from the original run
    new_run.vcs_commit_sha = run.vcs_commit_sha
    new_run.vcs_branch = run.vcs_branch
    new_run.vcs_pull_request_number = run.vcs_pull_request_number
    new_run.module_overrides = run.module_overrides

    new_run = await run_service.queue_run(db, new_run)
    await db.commit()

    return JSONResponse(content=_run_json(new_run), status_code=201)


# ── Phase Status Mapping ─────────────────────────────────────────────────


def _plan_status(run: Run) -> str:
    """Map run status to go-tfe plan phase status."""
    s = run.status
    if s in ("pending", "queued"):
        return "pending"
    if s == "planning":
        return "running"
    if s in ("planned", "confirmed", "applying", "canceling", "applied"):
        # `canceling` only arises from `applying`, by which point the
        # plan phase is finished — report it accordingly.
        return "finished"
    if s == "errored":
        # Errored during plan phase (plan never finished)
        if run.plan_finished_at is None:
            return "errored"
        return "finished"
    if s in ("canceled", "discarded"):
        return "canceled"
    return s


def _apply_status(run: Run) -> str:
    """Map run status to go-tfe apply phase status."""
    s = run.status
    if s in ("pending", "queued", "planning", "planned"):
        return "unreachable"
    if s == "confirmed":
        return "pending"
    if s in ("applying", "canceling"):
        # `canceling` is "apply is in flight and being killed". From the
        # phase-status view it's still running until the reconciler
        # resolves it to a terminal.
        return "running"
    if s == "applied":
        return "finished"
    if s == "errored":
        # Errored during apply phase (apply was started but never finished)
        if run.apply_started_at and not run.apply_finished_at:
            return "errored"
        return "unreachable"
    if s in ("canceled", "discarded"):
        return "canceled"
    return s


# ── Run Events (go-tfe compatibility) ────────────────────────────────────


@router.get("/runs/{run_id}/run-events")
async def list_run_events(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List run events (status transitions) for go-tfe compatibility.

    go-tfe uses this endpoint to track run progress during cloud runs.
    We synthesize events from the run's status timestamps.
    """
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)

    events = []
    event_pairs = [
        ("queued", run.created_at),
        ("planning", run.plan_started_at),
        ("planned", run.plan_finished_at),
        ("applying", run.apply_started_at),
        ("applied", run.apply_finished_at),
    ]

    for i, (action, ts) in enumerate(event_pairs):
        if ts is None:
            continue
        events.append(
            {
                "id": f"re-{run.id}-{i}",
                "type": "run-events",
                "attributes": {
                    "action": action,
                    "created-at": _rfc3339(ts),
                },
                "relationships": {
                    "run": {"data": {"id": f"run-{run.id}", "type": "runs"}},
                },
            }
        )

    return JSONResponse(content={"data": events})


# ── Plan & Apply Details ─────────────────────────────────────────────────


def _plan_json(run: Run) -> dict:
    """Build plan JSON:API response for a run."""
    base = settings.auth.callback_base_url.rstrip("/")
    attrs: dict = {
        "status": _plan_status(run),
        "log-read-url": f"{base}/api/v2/plans/{run.id}/log",
        "has-changes": run.status in ("planned", "confirmed", "applying", "applied"),
    }
    if run.has_json_output:
        attrs["json-output"] = f"{base}/api/v2/plans/{run.id}/json-output"
    # TFE-named counts on the Plan resource (additions / changes /
    # destructions / imports). Replacements have no TFE-equivalent so
    # the UI gets that from the Run resource instead. Surfaced only
    # when the runner has uploaded and we parsed successfully.
    if run.resource_additions is not None:
        attrs["resource-additions"] = run.resource_additions
        attrs["resource-changes"] = run.resource_changes
        attrs["resource-destructions"] = run.resource_destructions
        attrs["resource-imports"] = run.resource_imports
    # AI plan summary URL — surfaced as a Terrapod-native link on every
    # plan response so the UI knows where to fetch the structured
    # summary. 404s gracefully when no summary exists yet. Omitted
    # entirely when the feature is globally disabled so the UI
    # doesn't make a doomed fetch for every page load (#463 phase 7).
    if settings.ai_summary.enabled:
        attrs["ai-summary-url"] = f"{base}/api/terrapod/v1/runs/{run.id}/plan-summary"
    return {
        "data": {
            "id": f"plan-{run.id}",
            "type": "plans",
            "attributes": attrs,
            "links": {
                "self": f"/api/v2/plans/plan-{run.id}",
            },
        }
    }


@router.get("/plans/{plan_id}")
async def show_plan_by_id(
    plan_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show plan details by plan ID (go-tfe compatibility).

    go-tfe fetches plans via GET /api/v2/plans/{plan_id} during cloud runs.
    Plan IDs use the same UUID as the run with a 'plan-' prefix.
    """
    run = await _get_run(plan_id.replace("plan-", "run-"), db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)
    return JSONResponse(content=_plan_json(run))


@extensions_router.get("/runs/{run_id}/plan-summary")
async def show_plan_summary(
    run_id: str = Path(...),
    locale: str | None = Query(None),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """AI-generated plan summary or failure analysis (#401).

    Terrapod-native — not part of the TFE CLI surface. Lives under
    ``/api/terrapod/v1/`` alongside the other run extensions.

    Returns 404 when no summary exists yet — the UI uses this as the
    "not summarised" signal (vs a `pending` row which means "in flight").
    The response shape is the same for both ``kind`` values; the UI
    branches on the ``kind`` attribute.

    The stored text is authoritative and in the deployment's
    ``ai_summary.summary_language`` (#767). When the caller passes a
    ``?locale=`` for a different real language, the description and risk
    factors are translated on view (Redis-cached, 7-day sliding TTL) and
    ``translated``/``language`` attributes reflect that. Translation is
    best-effort: on failure or budget exhaustion the canonical text is
    served with ``translated=false``.
    """
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)

    summary = (
        await db.execute(select(PlanSummary).where(PlanSummary.run_id == run.id))
    ).scalar_one_or_none()
    if summary is None:
        raise HTTPException(status_code=404, detail="no summary for this plan")

    canonical_language = settings.ai_summary.summary_language
    description = summary.description
    risk_factors = summary.risk_factors
    translated = False
    if summary.status == "ready" and locale:
        from terrapod.services import summary_translation

        result = await summary_translation.translate_summary(
            summary_id=str(summary.id),
            description=summary.description,
            risk_factors=summary.risk_factors,
            reader_locale=locale,
        )
        if result is not None:
            description = result["description"]
            risk_factors = result["risk_factors"]
            translated = True

    return JSONResponse(
        content={
            "data": {
                "id": f"plan-summary-{summary.id}",
                "type": "plan-summaries",
                "attributes": {
                    "kind": summary.kind,
                    "status": summary.status,
                    "description": description,
                    "risk-level": summary.risk_level,
                    "risk-factors": risk_factors,
                    "language": canonical_language,
                    "translated": translated,
                    "model": summary.model,
                    "input-tokens": summary.input_tokens,
                    "output-tokens": summary.output_tokens,
                    "error-message": summary.error_message,
                    "created-at": _rfc3339(summary.created_at),
                    "updated-at": _rfc3339(summary.updated_at),
                },
                "relationships": {
                    "plan": {"data": {"id": f"plan-{run.id}", "type": "plans"}},
                    "run": {"data": {"id": f"run-{run.id}", "type": "runs"}},
                },
            }
        }
    )


@extensions_router.get("/runs/{run_id}/impact-graph")
async def show_impact_graph(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Compact plan dependency graph for the run-page Impact graph (#761).

    Terrapod-native. Derives ``{nodes, edges, meta}`` server-side from the
    run's stored plan JSON (see ``plan_graph_service``) and returns it inline —
    small payload, browser-reachable through the BFF in every storage backend
    (unlike ``/plans/{id}/json-output``, which 302s to a presigned URL that the
    browser can't reach with the filesystem backend). 404 when the run produced
    no JSON plan output.
    """
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)

    graph = await plan_graph_service.get_impact_graph(run)
    if graph is None:
        raise HTTPException(status_code=404, detail="no plan graph for this run")

    return JSONResponse(
        content={
            "data": {
                "id": f"impact-graph-{run.id}",
                "type": "impact-graphs",
                "attributes": graph,
                "relationships": {
                    "run": {"data": {"id": f"run-{run.id}", "type": "runs"}},
                },
            }
        }
    )


@extensions_router.get("/runs/{run_id}/cost-estimate")
async def show_cost_estimate(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Cost estimate for the run-page Cost tab (#871).

    Terrapod-native. Returns the runner-produced ``cost_estimate.json`` (the
    native cost estimate of the plan's monthly cost delta)
    inline — small payload, browser-reachable through the BFF in every storage
    backend (unlike a presigned 302). 404 when the run produced no estimate
    (errored before plan, cost estimation disabled, or the artifact aged out).
    Every figure here is **data** (engine-derived); no AI is involved.
    """
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)

    if not run.has_cost_estimate:
        raise HTTPException(status_code=404, detail="no cost estimate for this run")

    storage = get_storage()
    key = cost_estimate_key(str(run.workspace_id), str(run.id))
    try:
        raw = await storage.get(key)
    except ObjectNotFoundError:
        raise HTTPException(status_code=404, detail="no cost estimate for this run") from None
    # Small artifact (bounded by resource count) — inline parse is fine here.
    estimate = json.loads(raw)

    # Advertise the AI cost-narrative URL only when the feature is globally
    # enabled (#871) — this omission is the UI's "is the cost AI on?" signal,
    # mirroring how `ai-summary-url` gates the plan summary. AI is polish only;
    # the figures above stay authoritative regardless.
    if settings.ai_summary.enabled:
        estimate = {**estimate, "ai-summary-url": f"/api/terrapod/v1/runs/{run.id}/cost-summary"}

    return JSONResponse(
        content={
            "data": {
                "id": f"cost-estimate-{run.id}",
                "type": "cost-estimates",
                "attributes": estimate,
                "relationships": {
                    "run": {"data": {"id": f"run-{run.id}", "type": "runs"}},
                },
            }
        }
    )


def _cost_summary_json(
    run_id,
    summary: CostSummary,
    *,
    estimated_resources: list | None = None,
    narrative: str | None = None,
    advisories: list | None = None,
    translated: bool = False,
    language: str = "",
) -> dict:
    """JSON:API body for a cost-summary row.

    The natural-language fields default to the stored (canonical-language)
    values; pass translated overrides (with ``translated=True`` + the canonical
    ``language``) to serve a reader-locale view (#871).
    """
    return {
        "data": {
            "id": f"cost-summary-{summary.id}",
            "type": "cost-summaries",
            "attributes": {
                "status": summary.status,
                # PRIMARY (#871): the model's estimates for what the engine couldn't
                # price. Each carries source="ai-estimate" (stamped server-side);
                # the UI renders these as a separate overlay, never summed into
                # the authoritative deterministic total.
                "estimated-resources": estimated_resources
                if estimated_resources is not None
                else summary.estimated_resources,
                "narrative": narrative if narrative is not None else summary.narrative,
                # Advisories also carry source="ai-estimate".
                "advisories": advisories if advisories is not None else summary.advisories,
                "model": summary.model,
                "input-tokens": summary.input_tokens,
                "output-tokens": summary.output_tokens,
                "error-message": summary.error_message,
                # Canonical language the summary is stored in; `translated` is
                # true when the served prose was translated on view (#871/#767).
                "language": language,
                "translated": translated,
                "created-at": _rfc3339(summary.created_at),
                "updated-at": _rfc3339(summary.updated_at),
            },
            "relationships": {
                "run": {"data": {"id": f"run-{run_id}", "type": "runs"}},
            },
        }
    }


@extensions_router.get("/runs/{run_id}/cost-summary")
async def show_cost_summary(
    run_id: str = Path(...),
    locale: str | None = Query(None),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """AI cost estimate + savings advisories for a run (#871).

    Terrapod-native; the optional AI *enhancement* over the data-only cost
    estimate, riding the plan-analysis AI switch. Its estimates price what the engine
    couldn't; the authoritative deterministic figures live on the data-only
    `/runs/{id}/cost-estimate` endpoint and are never restated here. Every AI
    dollar amount is tagged `source: "ai-estimate"`.

    404 when no cost estimate exists yet — the UI uses this as the "not
    generated" signal (vs a `pending` row, which means "in flight"). The prose
    is stored in the deployment's `ai_summary.summary_language`; when the caller
    passes a `?locale=` for a different real language, the narrative + each
    estimate's `basis` + each advisory's `title`/`detail` are translated on view
    (Redis-cached, 7-day sliding TTL, best-effort) and `translated`/`language`
    reflect that. On failure or budget exhaustion the canonical text is served
    with `translated=false`.
    """
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)

    summary = (
        await db.execute(select(CostSummary).where(CostSummary.run_id == run.id))
    ).scalar_one_or_none()
    if summary is None:
        raise HTTPException(status_code=404, detail="no cost estimate for this run")

    canonical_language = settings.ai_summary.summary_language
    if summary.status == "ready" and locale:
        from terrapod.services import summary_translation

        result = await summary_translation.translate_cost_summary(
            summary_id=str(summary.id),
            narrative=summary.narrative,
            estimated_resources=summary.estimated_resources,
            advisories=summary.advisories,
            reader_locale=locale,
        )
        if result is not None:
            return JSONResponse(
                content=_cost_summary_json(
                    run.id,
                    summary,
                    estimated_resources=result["estimated_resources"],
                    narrative=result["narrative"],
                    advisories=result["advisories"],
                    translated=True,
                    language=canonical_language,
                )
            )

    return JSONResponse(content=_cost_summary_json(run.id, summary, language=canonical_language))


@extensions_router.post("/runs/{run_id}/cost-summary/regenerate")
async def regenerate_cost_summary(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Re-fire the AI cost narrative for a run (#871).

    Anyone with workspace `read` can regenerate; it mutates no infrastructure
    and is centrally cost-gated by `ai_summary.daily_token_budget`. Upserts the
    row to `pending`, enqueues the `ai_cost_summary` trigger BYPASSING the
    per-run dedup (an explicit operator click always goes through), returns 202.

    409 when the run has no cost estimate to narrate. 503 when AI is globally
    disabled.
    """
    from terrapod.services.scheduler import enqueue_trigger

    if not settings.ai_summary.enabled:
        raise HTTPException(status_code=503, detail="AI summary is disabled globally")

    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)

    if not run.has_cost_estimate:
        raise HTTPException(status_code=409, detail="run has no cost estimate to narrate")

    pending_values = {
        "run_id": run.id,
        "status": "pending",
        "model": settings.ai_summary.model,
        "estimated_resources": [],
        "narrative": "",
        "advisories": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "error_message": "",
    }
    stmt = pg_insert(CostSummary).values(**pending_values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["run_id"],
        set_={
            "status": stmt.excluded.status,
            "model": stmt.excluded.model,
            "estimated_resources": stmt.excluded.estimated_resources,
            "narrative": stmt.excluded.narrative,
            "advisories": stmt.excluded.advisories,
            "input_tokens": stmt.excluded.input_tokens,
            "output_tokens": stmt.excluded.output_tokens,
            "error_message": stmt.excluded.error_message,
            "updated_at": now_utc(),
        },
    )
    await db.execute(stmt)
    await db.commit()

    summary = (
        await db.execute(select(CostSummary).where(CostSummary.run_id == run.id))
    ).scalar_one_or_none()

    # No dedup_key → bypass the per-run dedup; an explicit click always fires.
    await enqueue_trigger("ai_cost_summary", {"run_id": str(run.id)})

    return JSONResponse(status_code=202, content=_cost_summary_json(run.id, summary))


# ── Cost-summary chat (#871) ─────────────────────────────────────────────


def _cost_summary_message_attr(msg: CostSummaryMessage) -> dict:
    """JSON:API attributes block for a single cost-chat turn."""
    return {
        "role": msg.role,
        "content": msg.content,
        "model": msg.model,
        "input-tokens": msg.input_tokens,
        "output-tokens": msg.output_tokens,
        "error-message": msg.error_message,
        "created-at": _rfc3339(msg.created_at),
    }


async def _resolve_cost_summary_for_chat(
    run_id: str, user: AuthenticatedUser, db: AsyncSession
) -> tuple[Run, CostSummary, Workspace]:
    """Shared header for both cost-chat endpoints — run exists, user has
    workspace `read`, a ready cost summary exists (404/409 otherwise)."""
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)
    summary = (
        await db.execute(select(CostSummary).where(CostSummary.run_id == run.id))
    ).scalar_one_or_none()
    if summary is None:
        raise HTTPException(status_code=404, detail="no cost estimate for this run")
    if summary.status != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"cost estimate is '{summary.status}', not 'ready' — cannot start chat",
        )
    workspace = (
        await db.execute(select(Workspace).where(Workspace.id == run.workspace_id))
    ).scalar_one_or_none()
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return run, summary, workspace


@extensions_router.get("/runs/{run_id}/cost-summary/messages")
async def list_cost_summary_messages(
    run_id: str = Path(...),
    locale: str | None = Query(None),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Full transcript of the AI cost-estimate chat thread (#871).

    Chronological order; empty list when no follow-ups posted yet. The initial
    estimate/advisories live on the parent ``CostSummary`` row — this returns
    ONLY the conversational follow-ups. With ``?locale=`` set to a different real
    language, each message is translated on view (best-effort; ``translated``
    per row).
    """
    _run, summary, _ws = await _resolve_cost_summary_for_chat(run_id, user, db)
    rows = (
        (
            await db.execute(
                select(CostSummaryMessage)
                .where(CostSummaryMessage.cost_summary_id == summary.id)
                .order_by(CostSummaryMessage.created_at, CostSummaryMessage.id)
            )
        )
        .scalars()
        .all()
    )

    translations: list[str | None] = [None] * len(rows)
    if locale and rows:
        from terrapod.services import summary_translation

        translations = await asyncio.gather(
            *(
                summary_translation.translate_message(
                    message_id=str(row.id), content=row.content, reader_locale=locale
                )
                for row in rows
            )
        )

    data = []
    for row, translated_content in zip(rows, translations, strict=True):
        attrs = _cost_summary_message_attr(row)
        attrs["translated"] = translated_content is not None
        if translated_content is not None:
            attrs["content"] = translated_content
        data.append(
            {
                "id": f"cost-summary-message-{row.id}",
                "type": "cost-summary-messages",
                "attributes": attrs,
            }
        )
    return JSONResponse(
        content={
            "data": data,
            "meta": {"count": len(rows), "language": settings.ai_summary.summary_language},
        }
    )


@extensions_router.post("/runs/{run_id}/cost-summary/messages")
async def post_cost_summary_message(
    run_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Post an operator follow-up + get the synchronous assistant reply (#871).

    Body: ``{"data": {"attributes": {"content": "...", "locale": "de"}}}``.
    ``locale`` (optional) is the reader's UI language: the incoming prompt is
    normalised into the system language so the stored thread stays monolingual,
    and the reply is translated back to the reader's locale for the response.
    Read-on-workspace auth (anyone who can see the run can chat). Returns 201.
    """
    from terrapod.services import summary_translation
    from terrapod.services.cost_summariser import post_cost_followup
    from terrapod.services.summariser import (
        FollowupBudgetExhausted,
        FollowupCapReached,
        FollowupDisabled,
        FollowupError,
    )

    content = ""
    locale = None
    try:
        attrs = body.get("data", {}).get("attributes", {}) or {}
        content = str(attrs.get("content", "") or "")
        raw_locale = attrs.get("locale")
        locale = str(raw_locale) if raw_locale else None
    except AttributeError:
        raise HTTPException(status_code=400, detail="malformed body") from None

    run, summary, workspace = await _resolve_cost_summary_for_chat(run_id, user, db)

    if locale:
        content = await summary_translation.normalize_to_system_language(content, locale)

    try:
        assistant_row = await post_cost_followup(
            db=db,
            cost_summary=summary,
            run=run,
            workspace=workspace,
            user_message_text=content,
        )
    except FollowupDisabled as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except FollowupCapReached as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except FollowupBudgetExhausted as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except FollowupError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=502, detail=f"model call failed: {e}") from e

    attrs = _cost_summary_message_attr(assistant_row)
    attrs["translated"] = False
    if locale:
        reply_tr = await summary_translation.translate_message(
            message_id=str(assistant_row.id), content=assistant_row.content, reader_locale=locale
        )
        if reply_tr is not None:
            attrs["content"] = reply_tr
            attrs["translated"] = True

    return JSONResponse(
        status_code=201,
        content={
            "data": {
                "id": f"cost-summary-message-{assistant_row.id}",
                "type": "cost-summary-messages",
                "attributes": attrs,
            }
        },
    )


def _summary_kind_for_run(run: Run) -> str | None:
    """Pick the right summariser kind for a run's current state.

    Returns "plan_summary" for runs that produced a plan (any state past
    `planning` except errored), "failure_analysis" for runs that
    errored during EITHER plan or apply (#419), and None when no
    summary kind applies (still in `pending`/`queued`/`planning`).
    """
    if run.status == "errored" and run.plan_started_at:
        return "failure_analysis"
    if run.status in {"planned", "confirmed", "applying", "applied", "discarded"}:
        return "plan_summary"
    return None


@extensions_router.post("/runs/{run_id}/plan-summary/regenerate")
async def regenerate_plan_summary(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Re-fire the AI summary for an existing run (#401 follow-up).

    Anyone with workspace `read` can regenerate; the call doesn't mutate
    infrastructure. Cost is centrally gated by `ai_summary.daily_token_budget`.

    The endpoint:
      • picks `kind` from current run state (plan_summary vs failure_analysis)
      • upserts the PlanSummary row to status='pending' synchronously
        so the UI immediately reflects the regenerate
      • enqueues the same `ai_plan_summary` trigger as the auto-fire path,
        BYPASSING the 5-min dedup (the user explicitly asked)
      • returns 202 with the now-pending row

    Returns 409 if the run has no summarisable state (still planning, or
    apply-phase errored). Returns 503 if AI summary is globally disabled.
    """
    from terrapod.config import settings as _settings
    from terrapod.services.scheduler import enqueue_trigger

    if not _settings.ai_summary.enabled:
        raise HTTPException(status_code=503, detail="AI summary is disabled globally")

    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)

    kind = _summary_kind_for_run(run)
    if kind is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"run is in state '{run.status}' — no summary kind applies "
                "(plan_summary needs a post-plan state; failure_analysis "
                "needs plan-phase errored)"
            ),
        )

    # Upsert to pending so the UI shows feedback immediately. Reuses the
    # same model field so the operator sees which model is being called.
    pending_values = {
        "run_id": run.id,
        "kind": kind,
        "status": "pending",
        "model": _settings.ai_summary.model,
        "description": "",
        "risk_level": "",
        "risk_factors": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "error_message": "",
    }
    stmt = pg_insert(PlanSummary).values(**pending_values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["run_id"],
        set_={
            "kind": stmt.excluded.kind,
            "status": stmt.excluded.status,
            "model": stmt.excluded.model,
            "description": stmt.excluded.description,
            "risk_level": stmt.excluded.risk_level,
            "risk_factors": stmt.excluded.risk_factors,
            "input_tokens": stmt.excluded.input_tokens,
            "output_tokens": stmt.excluded.output_tokens,
            "error_message": stmt.excluded.error_message,
            "updated_at": now_utc(),
        },
    )
    await db.execute(stmt)
    await db.commit()

    # Re-read so we return the fresh row (gives us the id + timestamps).
    summary = (
        await db.execute(select(PlanSummary).where(PlanSummary.run_id == run.id))
    ).scalar_one_or_none()

    # No dedup_key → bypass the 5-min auto-dedup. Operator clicks should
    # always go through, even when an automatic enqueue happened seconds
    # ago. Budget gating still applies on the handler side.
    await enqueue_trigger(
        "ai_plan_summary",
        {"run_id": str(run.id), "kind": kind},
    )

    # SSE so the run-detail page reverts to the pending placeholder
    # immediately on regenerate, without waiting for the handler to
    # fire its own pending event (#463 phase 4).
    try:
        from terrapod.services.summariser import _emit_summary_event

        await _emit_summary_event("plan_summary_pending", run.workspace_id, run.id)
    except Exception as e:  # SSE is best-effort
        logger.debug("Failed to publish pending event on regenerate", error=str(e))

    return JSONResponse(
        status_code=202,
        content={
            "data": {
                "id": f"plan-summary-{summary.id}",
                "type": "plan-summaries",
                "attributes": {
                    "kind": summary.kind,
                    "status": summary.status,
                    "description": summary.description,
                    "risk-level": summary.risk_level,
                    "risk-factors": summary.risk_factors,
                    "model": summary.model,
                    "input-tokens": summary.input_tokens,
                    "output-tokens": summary.output_tokens,
                    "error-message": summary.error_message,
                    "created-at": _rfc3339(summary.created_at),
                    "updated-at": _rfc3339(summary.updated_at),
                },
                "relationships": {
                    "plan": {"data": {"id": f"plan-{run.id}", "type": "plans"}},
                    "run": {"data": {"id": f"run-{run.id}", "type": "runs"}},
                },
            }
        },
    )


# ── Plan summary follow-up chat (#463) ─────────────────────────────────


def _plan_summary_message_attr(msg: PlanSummaryMessage) -> dict:
    """JSON:API attributes block for a single chat turn."""
    return {
        "role": msg.role,
        "content": msg.content,
        "model": msg.model,
        "input-tokens": msg.input_tokens,
        "output-tokens": msg.output_tokens,
        "error-message": msg.error_message,
        "created-at": _rfc3339(msg.created_at),
    }


async def _resolve_plan_summary_for_chat(
    run_id: str, user: AuthenticatedUser, db: AsyncSession
) -> tuple[Run, PlanSummary, Workspace]:
    """Shared header for both chat endpoints.

    Verifies the run exists, the user has workspace `read`, an
    initial summary has landed (404 otherwise — can't chat against a
    plan that hasn't been summarised), and returns the joined rows.
    """
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)
    summary = (
        await db.execute(select(PlanSummary).where(PlanSummary.run_id == run.id))
    ).scalar_one_or_none()
    if summary is None:
        raise HTTPException(status_code=404, detail="no summary for this plan")
    if summary.status != "ready":
        raise HTTPException(
            status_code=409,
            detail=f"initial summary is '{summary.status}', not 'ready' — cannot start chat",
        )
    workspace = (
        await db.execute(select(Workspace).where(Workspace.id == run.workspace_id))
    ).scalar_one_or_none()
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return run, summary, workspace


@extensions_router.get("/runs/{run_id}/plan-summary/messages")
async def list_plan_summary_messages(
    run_id: str = Path(...),
    locale: str | None = Query(None),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Full transcript of the AI plan-summary chat thread (#463).

    Returns the messages in chronological order. The initial
    structured summary lives on the parent `PlanSummary` row
    (`description` + `risk_factors`); this endpoint returns ONLY the
    conversational follow-ups. The UI renders message[0] from the
    parent summary and appends these.

    Empty list when no follow-ups have been posted yet.

    The stored thread is authoritative + monolingual in the deployment's
    ``ai_summary.summary_language`` (#767). With ``?locale=`` set to a
    different real language, each message's content is translated on view
    (Redis-cached, best-effort — canonical on failure); the per-message
    ``translated`` flag reflects whether that turn was translated.
    """
    _run, summary, _ws = await _resolve_plan_summary_for_chat(run_id, user, db)
    rows = (
        (
            await db.execute(
                select(PlanSummaryMessage)
                .where(PlanSummaryMessage.plan_summary_id == summary.id)
                .order_by(PlanSummaryMessage.created_at, PlanSummaryMessage.id)
            )
        )
        .scalars()
        .all()
    )

    # View-time translation of the transcript into the reader's locale.
    translations: list[str | None] = [None] * len(rows)
    if locale and rows:
        from terrapod.services import summary_translation

        translations = await asyncio.gather(
            *(
                summary_translation.translate_message(
                    message_id=str(row.id), content=row.content, reader_locale=locale
                )
                for row in rows
            )
        )

    data = []
    for row, translated_content in zip(rows, translations, strict=True):
        attrs = _plan_summary_message_attr(row)
        attrs["translated"] = translated_content is not None
        if translated_content is not None:
            attrs["content"] = translated_content
        data.append(
            {
                "id": f"plan-summary-message-{row.id}",
                "type": "plan-summary-messages",
                "attributes": attrs,
            }
        )
    return JSONResponse(
        content={
            "data": data,
            "meta": {
                "count": len(rows),
                "language": settings.ai_summary.summary_language,
            },
        }
    )


@extensions_router.post("/runs/{run_id}/plan-summary/messages")
async def post_plan_summary_message(
    run_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Post a user follow-up + get the synchronous assistant reply (#463).

    Body: ``{"data": {"attributes": {"content": "...", "locale": "de"}}}``
    (JSON:API shape). ``locale`` is optional and carries the reader's UI
    language (in the body, not a query string — it's request payload).

    The service path persists the user row first (so a failed model
    call still records what was asked), then calls the model with
    the cacheable prefix the initial summary used (Anthropic /
    Bedrock-Anthropic / Bedrock-Nova get the prompt-cache hit), then
    persists the assistant turn + telemetry.

    Language handling (#767): the stored thread is authoritative and
    monolingual in ``ai_summary.summary_language``. When ``locale`` is a
    different real language, the incoming prompt is NORMALISED into the
    system language before it joins the thread (keeps the thread + the
    prompt-cache prefix single-language); the model answers in the system
    language, and the reply is translated back to the reader's locale for
    the response only. Returns 201 with the assistant message attributes.
    Authorisation is read-on-workspace — anyone who can see the run can
    chat in its thread (matches PR conversation semantics).
    """
    from terrapod.services import summary_translation
    from terrapod.services.summariser import (
        FollowupBudgetExhausted,
        FollowupCapReached,
        FollowupDisabled,
        FollowupError,
        post_followup,
    )

    content = ""
    locale = None
    try:
        attrs = body.get("data", {}).get("attributes", {}) or {}
        content = str(attrs.get("content", "") or "")
        raw_locale = attrs.get("locale")
        locale = str(raw_locale) if raw_locale else None
    except AttributeError:
        raise HTTPException(status_code=400, detail="malformed body") from None

    run, summary, workspace = await _resolve_plan_summary_for_chat(run_id, user, db)

    # Normalise the prompt into the system language so the stored thread
    # stays monolingual/authoritative (no-op when reader == system language).
    if locale:
        content = await summary_translation.normalize_to_system_language(content, locale)

    try:
        assistant_row = await post_followup(
            db=db,
            plan_summary=summary,
            run=run,
            workspace=workspace,
            user_message_text=content,
        )
    except FollowupDisabled as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except FollowupCapReached as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except FollowupBudgetExhausted as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except FollowupError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except (RuntimeError, ValueError) as e:
        # Model HTTP / parse failure. The service has persisted both
        # the user turn AND an errored assistant turn, so the
        # transcript reflects the failure.
        raise HTTPException(status_code=502, detail=f"model call failed: {e}") from e

    # The stored reply is in the system language; translate it back to the
    # reader's locale for immediate display (the transcript GET does the same).
    attrs = _plan_summary_message_attr(assistant_row)
    attrs["translated"] = False
    if locale:
        reply_tr = await summary_translation.translate_message(
            message_id=str(assistant_row.id), content=assistant_row.content, reader_locale=locale
        )
        if reply_tr is not None:
            attrs["content"] = reply_tr
            attrs["translated"] = True

    return JSONResponse(
        status_code=201,
        content={
            "data": {
                "id": f"plan-summary-message-{assistant_row.id}",
                "type": "plan-summary-messages",
                "attributes": attrs,
            }
        },
    )


@extensions_router.get("/runs/{run_id}/plan")
async def show_plan(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show plan details including log URL."""
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)
    return JSONResponse(content=_plan_json(run))


def _apply_json(run: Run) -> dict:
    """Build apply JSON:API response for a run."""
    from terrapod.config import settings

    base = settings.auth.callback_base_url.rstrip("/")
    return {
        "data": {
            "id": f"apply-{run.id}",
            "type": "applies",
            "attributes": {
                "status": _apply_status(run),
                "log-read-url": f"{base}/api/v2/applies/{run.id}/log",
            },
            "links": {
                "self": f"/api/v2/applies/apply-{run.id}",
            },
        }
    }


@router.get("/applies/{apply_id}")
async def show_apply_by_id(
    apply_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show apply details by apply ID (go-tfe compatibility).

    go-tfe fetches applies via GET /api/v2/applies/{apply_id} during cloud runs.
    Apply IDs use the same UUID as the run with an 'apply-' prefix.
    """
    run = await _get_run(apply_id.replace("apply-", "run-"), db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)
    return JSONResponse(content=_apply_json(run))


@extensions_router.get("/runs/{run_id}/apply")
async def show_apply(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show apply details including log URL."""
    run = await _get_run(run_id, db)
    await _require_run_ws_capability(run, cap.RUN_READ, user, db)
    return JSONResponse(content=_apply_json(run))


# ── SSE (Server-Sent Events) ─────────────────────────────────────────────


@extensions_router.get("/workspaces/{workspace_id}/runs/events")
async def run_events_stream(
    request: Request,
    workspace_id: str = Path(...),
) -> EventSourceResponse:
    """Stream run status change events via SSE for real-time UI updates.

    Uses short-lived DB session for auth/RBAC check, then releases it
    before entering the long-lived SSE streaming loop. This prevents
    holding a DB pool connection for the entire SSE connection lifetime.
    """
    from terrapod.api.dependencies import authenticate_request
    from terrapod.db.session import get_db_session
    from terrapod.redis.client import RUN_EVENTS_PREFIX, subscribe_channel

    user = await authenticate_request(request)

    async with get_db_session() as db:
        ws = await _get_workspace(workspace_id, db)
        caps = await resolve_workspace_capabilities_for(db, user, ws)
        if not has_capability(caps, cap.RUN_READ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Requires read permission on workspace",
            )
        ws_id = str(ws.id)

    channel = f"{RUN_EVENTS_PREFIX}{ws_id}"
    pubsub = await subscribe_channel(channel)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message["type"] == "message":
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode()
                    payload = json.loads(data)
                    yield {
                        "event": payload.get("event", "run_status_change"),
                        "data": json.dumps(payload),
                    }
                else:
                    # Send keepalive comment every cycle when no messages
                    yield {"comment": "keepalive"}
                    await asyncio.sleep(1)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return EventSourceResponse(event_generator())


# ── Listener Run Queue ───────────────────────────────────────────────────


@extensions_router.get("/listeners/{listener_id}/runs/next")
async def next_run(
    listener_id: str = Path(...),
    identity: ListenerIdentity = Depends(get_listener_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Poll for the next queued run assigned to this listener.

    Auth: must present a valid client cert whose listener-id matches the
    path. Without this, anyone with a listener-id could claim runs out from
    under the real listener and gather their resolved variables (which
    include sensitive workspace vars).

    Returns 204 No Content if no run is available.
    """
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    if identity.listener_id != l_uuid:
        raise HTTPException(
            status_code=403,
            detail="Certificate does not match the listener id in the path",
        )
    listener = await agent_pool_service.get_listener(l_uuid)
    if listener is None:
        raise HTTPException(status_code=404, detail="Listener not found")

    claim = await run_service.claim_next_run(
        db,
        listener_id=l_uuid,
        pool_id=uuid.UUID(listener["pool_id"]),
        listener_name=listener.get("name", ""),
    )
    if claim is None:
        return Response(status_code=204)

    run, phase = claim

    # Fetch workspace once for variables + var_files
    ws = await db.get(Workspace, run.workspace_id)

    # Resolve workspace variables for injection into the runner Job
    from terrapod.services.variable_service import resolve_variables

    resolved = await resolve_variables(db, run.workspace_id)
    env_vars = [{"key": v.key, "value": v.value} for v in resolved if v.category == "env"]
    # `hcl` is forwarded so the runner renders the value correctly into the
    # generated terrapod.auto.tfvars (raw HCL expression vs quoted string). All
    # terraform vars — sensitive and not — are delivered uniformly via the
    # per-run vars Secret (mounted as the tfvars file), never as plaintext env;
    # there is no sensitivity split. See runner/phases/tfvars.py + the listener
    # vars Secret.
    terraform_vars = [
        # Both names on the wire (#1435): a runner up to N-2 minors behind
        # reads `hcl` and knows nothing of `structured`.
        {"key": v.key, "value": v.value, "structured": v.structured, "hcl": v.structured}
        for v in resolved
        if v.category == "terraform"
    ]

    # Private-git-module auth (#1028): git_http_auth / git_ssh_auth vars are
    # resolved into concrete credentials here (a `vcs_connection` source mints a
    # short-lived git-HTTPS token from the referenced VCS connection), then
    # delivered — like the vars/hooks — via the per-run Secret and materialized by
    # the runner's git_auth phase before `init` fetches modules. Never plaintext
    # in the Job spec; never in logs (the phase is log-safe by construction).
    from terrapod.services.git_auth_service import resolve_git_auth

    git_auth = await resolve_git_auth(db, resolved)

    # Resolve execution hooks associated with this workspace (#619). Delivered
    # alongside the vars via the per-run Secret; the runner runs each hook_point
    # at its boundary. Enforced-empty for local execution never reaches here
    # (next_run is the agent-mode listener endpoint).
    #
    # Kill-switch (#678): enforce `runners.hooksEnabled` HERE, server-side and
    # authoritatively — when disabled the API serves NO hooks, so a custom
    # listener that ignores the flag still never receives any hook script. The
    # bundled listener also drops hooks when building the Job (defense in depth).
    from terrapod.config import load_runner_config
    from terrapod.services.execution_hook_service import resolve_hooks_for_workspace

    if load_runner_config().hooks_enabled:
        execution_hooks = await resolve_hooks_for_workspace(db, run.workspace_id)
    else:
        execution_hooks = []

    await db.commit()

    run_data = _run_json(run)
    run_data["data"]["attributes"]["env-vars"] = env_vars
    run_data["data"]["attributes"]["terraform-vars"] = terraform_vars
    run_data["data"]["attributes"]["execution-hooks"] = execution_hooks
    run_data["data"]["attributes"]["git-auth"] = git_auth
    # Cost estimation (#871): the API instructs the runner (via the listener)
    # whether to estimate cost — the runner never self-configures. Global API
    # setting today (per-workspace override is a future refinement); the runner
    # falls back to enabled if a lagging listener drops the field.
    run_data["data"]["attributes"]["cost-estimation"] = settings.cost_estimation.enabled
    run_data["data"]["attributes"]["cost-default-region"] = settings.cost_estimation.default_region
    run_data["data"]["attributes"]["var-files"] = ws.var_files if ws and ws.var_files else []
    run_data["data"]["attributes"]["working-directory"] = ws.working_directory if ws else ""
    run_data["data"]["attributes"]["phase"] = phase

    # Onboarding discovery (#824 P2): surface the session's provider + selected
    # types so the Job can run terrapod-query. The run stays plan-phase; the
    # runner branches on the presence of a session id. Resolved via the discovery
    # run id (== this run).
    from terrapod.db.models import ONBOARDING_DISCOVERY_SOURCE, OnboardingSession

    if run.source == ONBOARDING_DISCOVERY_SOURCE:
        sess = (
            await db.execute(
                select(OnboardingSession).where(OnboardingSession.discovery_run_id == run.id)
            )
        ).scalar_one_or_none()
        if sess is not None:
            attrs = run_data["data"]["attributes"]
            attrs["onboard-session-id"] = str(sess.id)
            attrs["onboard-provider"] = sess.provider
            attrs["onboard-provider-version"] = sess.provider_version or ""
            attrs["onboard-types"] = sess.selected_types or []

    return JSONResponse(content=run_data)


@extensions_router.patch("/listeners/{listener_id}/runs/{run_id}")
async def update_run_status(
    listener_id: str = Path(...),
    run_id: str = Path(...),
    body: dict = Body(...),
    identity: ListenerIdentity = Depends(get_listener_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Listener reports run status update."""
    run = await _get_run(run_id, db)

    # Verify this listener owns the run AND its cert matches the path
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    if identity.listener_id != l_uuid:
        raise HTTPException(
            status_code=403,
            detail="Certificate does not match the listener id in the path",
        )
    if run.listener_id != l_uuid:
        raise HTTPException(status_code=403, detail="Run not assigned to this listener")

    target_status = body.get("status", "")
    error_message = body.get("error_message", "")
    has_changes = body.get("has_changes")

    if not target_status:
        raise HTTPException(status_code=422, detail="status is required")

    # Listener is reporting a pre-launch failure if it's transitioning a run
    # from planning/applying → errored while job_name is still NULL (no Job
    # was ever created). Surface that in metrics so we can alert on bursts of
    # listener failures (auth / K8s outage / secret-create) without grepping
    # logs across replicas.
    if (
        target_status == "errored"
        and run.job_name is None
        and run.status in ("planning", "applying")
    ):
        from terrapod.api.metrics import LISTENER_LAUNCH_FAILURES

        LISTENER_LAUNCH_FAILURES.inc()

    # Set has_changes before transition (so it's visible in drift handler)
    if has_changes is not None:
        run.has_changes = has_changes

    try:
        if target_status == "planned" and not run.plan_only:
            # Route a plan completion through the shared, guarded path — the
            # SAME one the plan-result endpoint (report_plan_result) and the
            # reconciler use — so it applies the post-plan gates, the no-op
            # short-circuit, AND the #646/#647 auto-apply staleness +
            # manual-lock guards. A bare transition_run(..., "confirmed") here
            # bypassed those guards and could auto-apply a plan against state
            # that moved since it was computed (#665). complete_plan is
            # idempotent (no-ops unless the run is still `planning`).
            run = await run_service.complete_plan(db, run, has_changes=has_changes)
        else:
            run = await run_service.transition_run(
                db, run, target_status, error_message=error_message
            )

        # Unlock workspace when plan-only run reaches planned
        # (plan-only runs don't mutate state, so no need to hold the lock)
        if target_status == "planned" and run.plan_only:
            ws = await db.get(Workspace, run.workspace_id)
            if ws and ws.locked:
                ws.locked = False
                ws.lock_id = None

        # Unlock workspace on terminal state
        if target_status in run_service.TERMINAL_STATES:
            ws = await db.get(Workspace, run.workspace_id)
            if ws and ws.locked:
                ws.locked = False
                ws.lock_id = None

        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    return JSONResponse(content=_run_json(run))


# ── Runner Token ──────────────────────────────────────────────────────


@extensions_router.post("/listeners/{listener_id}/runs/{run_id}/runner-token")
async def create_runner_token(
    listener_id: str = Path(...),
    run_id: str = Path(...),
    body: dict = Body(default={}),
    listener: object = Depends(get_listener_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Generate a short-lived runner token for a run.

    Called by the listener after claiming a run. The token authenticates
    runner Job API calls (binary cache, provider mirror, artifact upload/download).
    """
    from terrapod.auth.runner_tokens import generate_runner_token
    from terrapod.config import load_runner_config

    run = await _get_run(run_id, db)

    # Verify this listener owns the run
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    if run.listener_id != l_uuid:
        raise HTTPException(status_code=403, detail="Run not assigned to this listener")

    config = load_runner_config()
    requested_ttl = body.get("ttl", config.token_ttl_seconds)
    token = generate_runner_token(run.id, ttl=requested_ttl)

    # Compute actual TTL (may have been clamped)
    max_ttl = config.max_token_ttl_seconds
    actual_ttl = min(requested_ttl, max_ttl) if max_ttl > 0 else requested_ttl

    return JSONResponse(content={"token": token, "expires_in": actual_ttl})


# ── Job Lifecycle Callbacks ───────────────────────────────────────────────


@extensions_router.post("/listeners/{listener_id}/runs/{run_id}/job-launched")
async def report_job_launched(
    listener_id: str = Path(...),
    run_id: str = Path(...),
    body: dict = Body(...),
    identity: ListenerIdentity = Depends(get_listener_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Listener reports Job creation for a run.

    After the listener creates a K8s Job + auth Secret, it calls this endpoint
    to register the Job name and namespace. The API uses this to track the Job
    and query its status via the reconciler.
    """
    run = await _get_run(run_id, db)

    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    if identity.listener_id != l_uuid:
        raise HTTPException(
            status_code=403,
            detail="Certificate does not match the listener id in the path",
        )
    if run.listener_id != l_uuid:
        raise HTTPException(status_code=403, detail="Run not assigned to this listener")

    job_name = body.get("job_name", "")
    job_namespace = body.get("job_namespace", "")
    if not job_name:
        raise HTTPException(status_code=422, detail="job_name is required")

    run.job_name = job_name
    run.job_namespace = job_namespace
    await db.commit()

    return JSONResponse(content={"status": "ok"})


@extensions_router.post("/listeners/{listener_id}/runs/{run_id}/job-status")
async def report_job_status(
    listener_id: str = Path(...),
    run_id: str = Path(...),
    body: dict = Body(...),
    identity: ListenerIdentity = Depends(get_listener_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Listener reports Job status in response to a check_job_status event.

    The reconciler publishes check_job_status events via SSE. The listener
    queries K8s for the Job status and POSTs the result here. The reconciler
    picks up the status from Redis on its next cycle.
    """
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    if identity.listener_id != l_uuid:
        raise HTTPException(
            status_code=403,
            detail="Certificate does not match the listener id in the path",
        )
    run = await _get_run(run_id, db)

    job_status = body.get("status", "")
    if not job_status:
        raise HTTPException(status_code=422, detail="status is required")

    phase = body.get("phase", "plan")

    from terrapod.redis.client import set_job_status

    await set_job_status(str(run.id), phase, job_status)

    # When the listener reports a failed Job, it MAY also send the container
    # exit code + K8s termination reason from the terminated pod (#430). Map
    # K8s reasons to the typed `runner_exit_status` bucket here — single
    # mapping point so the reconciler + UI + AI gate all agree.
    exit_code = body.get("exit_code")
    reason = body.get("reason", "")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        run.runner_exit_code = exit_code
    if isinstance(reason, str) and reason:
        run.runner_exit_reason = reason[:50]  # column is VARCHAR(50)

    # Compute the typed status from whatever signals we have.
    # - K8s `OOMKilled` reason is the authoritative OOM signal.
    # - Exit 137 without an explicit reason (pod GC'd before we observed)
    #   is "killed" — could be OOM, could be eviction, can't be sure.
    # - Other non-zero exits are "error".
    # - Exit 0 is "clean" (runner script exited cleanly, plan flow finished).
    # Only set runner_exit_status when we have a definitive signal — leaving
    # it "" means "pre-feature run / status unknown" and the UI shows no
    # banner.
    if reason == "OOMKilled":
        run.runner_exit_status = "oom"
    elif isinstance(exit_code, int) and not isinstance(exit_code, bool):
        if exit_code == 137:
            run.runner_exit_status = "killed"
        elif exit_code != 0:
            run.runner_exit_status = "error"
        else:
            run.runner_exit_status = "clean"

    await db.commit()

    return JSONResponse(content={"status": "ok"})


@extensions_router.put("/listeners/{listener_id}/runs/{run_id}/log-stream")
async def upload_log_stream(
    listener_id: str = Path(...),
    run_id: str = Path(...),
    phase: str = Query("plan"),
    request: Request = ...,
    identity: ListenerIdentity = Depends(get_listener_identity),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Listener uploads live pod log data for an in-progress run.

    The reconciler publishes stream_logs events via SSE. The listener reads
    pod logs from K8s and PUTs them here. The API stores the data in Redis
    and serves it from the log endpoints until the final log is uploaded to
    object storage by the runner Job.

    Phase is passed explicitly via query param to prevent late-arriving plan
    log data from being stored under the apply phase key when the run has
    already transitioned.
    """
    l_uuid = uuid.UUID(listener_id.removeprefix("listener-"))
    if identity.listener_id != l_uuid:
        raise HTTPException(
            status_code=403,
            detail="Certificate does not match the listener id in the path",
        )
    run = await _get_run(run_id, db)
    if phase not in ("plan", "apply"):
        phase = "plan" if run.status == "planning" else "apply"
    body_bytes = await request.body()

    from terrapod.redis.client import (
        LOG_STREAM_PREFIX,
        RUN_EVENTS_PREFIX,
        get_redis_client,
        publish_event,
    )

    redis = get_redis_client()
    # decode_responses=True on the Redis client means a bytes value
    # going through SETEX gets stringified via str(bytes) — yielding
    # the literal `b'…\\n…'` text. Decode to UTF-8 str so the value
    # round-trips cleanly. errors=replace covers any stray non-UTF-8
    # bytes (e.g. from a stack-trace's escape sequence).
    body_text = body_bytes.decode("utf-8", errors="replace")
    await redis.setex(f"{LOG_STREAM_PREFIX}{run.id}:{phase}", 300, body_text)

    # Notify frontend that fresh log data is available
    try:
        payload = json.dumps(
            {
                "event": "log_updated",
                "run_id": str(run.id),
                "workspace_id": str(run.workspace_id),
                "phase": phase,
            }
        )
        await publish_event(f"{RUN_EVENTS_PREFIX}{run.workspace_id}", payload)
    except Exception:
        pass  # Never let SSE publishing break the log upload

    return Response(status_code=204)


@extensions_router.post("/runs/{run_id}/plan-result")
async def report_plan_result(
    run_id: str = Path(...),
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Runner Job reports plan completion.

    Authoritative state-transition trigger: the runner has the exit code
    and the diff in hand, so its successful POST here is sufficient
    evidence that the plan finished. We update `has_changes` and drive the
    `planning → planned` (or `applied` for zero-change non-speculative)
    transition directly via `run_service.complete_plan`.

    The reconciler's listener-driven path remains as a fallback for cases
    where the runner can't post (OOM-killed before the entrypoint runs,
    network partition, etc.). Both paths land in the same idempotent helper
    so whichever wins, the second is a no-op.
    """
    run = await _get_run(run_id, db)

    has_changes = body.get("has_changes")
    await run_service.complete_plan(db, run, has_changes=has_changes)
    await db.commit()

    return JSONResponse(content={"status": "ok"})


@extensions_router.post("/runs/{run_id}/apply-result")
async def report_apply_result(
    run_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Runner Job reports apply completion.

    Mirror of `plan-result`: authoritative transition trigger driven by the
    runner's exit. Drives `applying → applied` via `run_service.complete_apply`,
    which is idempotent against the listener-driven fallback.
    """
    run = await _get_run(run_id, db)
    await run_service.complete_apply(db, run)
    await db.commit()

    return JSONResponse(content={"status": "ok"})


# ── Log Streaming Endpoints ──────────────────────────────────────────────

# These endpoints serve raw log content compatible with the go-tfe LogReader
# protocol.  No auth — the URL is a capability token (matches presigned URL
# pattern; go-tfe's LogReader does not send Authorization headers).

_STX = b"\x02"
_ETX = b"\x03"

_POST_PLAN_STATES = frozenset(
    {
        "planned",
        "confirmed",
        "applying",
        "applied",
        "errored",
        "discarded",
        "canceled",
    }
)


@router.get("/plans/{plan_id}/log")
async def plan_log(
    plan_id: str = Path(...),
    offset: int = Query(0),
    limit: int = Query(0),
    format: Literal["raw", "plain"] = Query("raw"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Stream plan log content (go-tfe LogReader compatible)."""
    try:
        run_uuid = uuid.UUID(plan_id.removeprefix("plan-").removeprefix("run-"))
    except ValueError:
        raise HTTPException(status_code=404, detail="Plan not found") from None
    run = await run_service.get_run(db, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    return await _serve_log(
        run=run,
        log_key=plan_log_key(str(run.workspace_id), str(run.id)),
        phase_complete_states=_POST_PLAN_STATES,
        offset=offset,
        limit=limit,
        strip_ansi=format == "plain",
    )


@router.get("/plans/{plan_id}/json-output")
async def plan_json_output(
    plan_id: str = Path(...),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Serve the structured JSON plan output (`tofu show -json`).

    go-tfe's `Plans.ReadJSONOutput` consumes this endpoint via
    `tofu show -json` against a remote run. Response is raw JSON bytes;
    auth is by capability (the plan UUID), matching `/plans/{id}/log`.
    A 302 to a presigned storage URL is fine — `req.Do` follows
    redirects.
    """
    try:
        run_uuid = uuid.UUID(plan_id.removeprefix("plan-").removeprefix("run-"))
    except ValueError:
        raise HTTPException(status_code=404, detail="Plan not found") from None
    run = await run_service.get_run(db, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    # Fast path: the flag is the source of truth. Avoid a storage call
    # for runs that never produced JSON (errored, older, upload failed).
    if not run.has_json_output:
        raise HTTPException(status_code=404, detail="JSON plan output not available")

    storage = get_storage()
    key = plan_json_output_key(str(run.workspace_id), str(run.id))
    # Belt-and-braces: retention may have deleted the artifact while
    # leaving the flag intact, in which case we must not redirect to a
    # signed URL pointing at a missing object.
    if not await storage.exists(key):
        raise HTTPException(status_code=404, detail="JSON plan output not available")
    url = await storage.presigned_get_url(key)
    return RedirectResponse(url=url.url, status_code=302)


@router.get("/applies/{apply_id}/log")
async def apply_log(
    apply_id: str = Path(...),
    offset: int = Query(0),
    limit: int = Query(0),
    format: Literal["raw", "plain"] = Query("raw"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Stream apply log content (go-tfe LogReader compatible)."""
    try:
        run_uuid = uuid.UUID(apply_id.removeprefix("apply-").removeprefix("run-"))
    except ValueError:
        raise HTTPException(status_code=404, detail="Apply not found") from None
    run = await run_service.get_run(db, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Apply not found")

    return await _serve_log(
        run=run,
        log_key=apply_log_key(str(run.workspace_id), str(run.id)),
        phase_complete_states=frozenset({"applied", "errored", "discarded", "canceled"}),
        offset=offset,
        limit=limit,
        strip_ansi=format == "plain",
        phase="apply",
    )


_ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[a-zA-Z]")


async def _serve_log(
    run: Run,
    log_key: str,
    phase_complete_states: frozenset[str],
    offset: int,
    limit: int,
    strip_ansi: bool = False,
    phase: str = "plan",
) -> Response:
    """Shared log serving logic with STX/ETX framing.

    Data source priority:
    1. Object storage (authoritative — final log uploaded by Job on completion)
    2. Redis live stream (live-streamed data from listener during execution)
    3. Empty response (no data available yet — client retries)

    ETX (end-of-log sentinel that tells the UI to stop polling) is appended
    ONLY when the data came from object storage. The Redis live-stream
    snapshot is inherently incomplete — the runner uploads the
    authoritative log to storage from its EXIT trap, which fires AFTER it
    POSTs plan-result and the run transitions to terminal. If we appended
    ETX on the Redis path the moment `phase_done` flipped true, the UI
    would stop polling before the EXIT trap landed the trailing bytes
    (typically the entrypoint's `PLAN_HAS_CHANGES=...` line and post-plan
    OPA output). The user then has to refresh to see the tail.
    """
    storage = get_storage()
    phase_done = run.status in phase_complete_states
    from_storage = False

    try:
        data = await storage.get(log_key)
        from_storage = True
    except ObjectNotFoundError:
        # Try live-streamed data from Redis (available for both in-progress
        # and recently-completed runs where the Job didn't upload final logs)
        try:
            from terrapod.redis.client import LOG_STREAM_PREFIX, get_redis_client

            redis = get_redis_client()
            live_data = await redis.get(f"{LOG_STREAM_PREFIX}{run.id}:{phase}")
            if live_data is not None:
                if isinstance(live_data, str):
                    live_data = live_data.encode()
                data = live_data
            elif phase_done:
                # Phase finished and neither storage nor Redis has anything —
                # nothing more is ever coming. Send ETX so the UI gives up.
                return Response(content=_STX + _ETX, media_type="text/plain")
            else:
                # Still running, no log yet — return empty (client retries)
                return Response(content=b"", media_type="text/plain")
        except Exception:
            if phase_done:
                return Response(content=_STX + _ETX, media_type="text/plain")
            return Response(content=b"", media_type="text/plain")

    if strip_ansi:
        data = _ANSI_RE.sub(b"", data)

    if limit > 0:
        chunk = data[offset : offset + limit]
    else:
        chunk = data[offset:]
    result = b""
    if offset == 0:
        result += _STX
    result += chunk
    # Append ETX only when serving from storage (authoritative final log)
    # AND the client has consumed everything. Redis-served data can be a
    # mid-flight snapshot even when phase_done is true; closing the stream
    # there truncates the visible log.
    if from_storage and phase_done and (limit == 0 or offset + limit >= len(data)):
        result += _ETX
    return Response(content=result, media_type="text/plain")
