"""Policy set + policy CRUD and run policy-evaluation endpoints (#343).

Terrapod-native management surface for OPA policy-as-code enforcement.
Policy sets are admin-managed; their Rego policies are validated with
``opa check`` at write time so broken Rego is rejected up front rather
than at run time. Run policy evaluations are readable by anyone with
read on the run's workspace; the override action requires workspace
admin.

UX CONTRACT: consumed by the web frontend — the admin policy-sets page
and the run-detail policy panel. Changes to response shapes, attribute
names, or status codes here MUST be matched by those pages.

Endpoints (all under /api/terrapod/v1):
    Management (admin):
        GET    /policy-sets                         list
        POST   /policy-sets                         create
        GET    /policy-sets/{id}                    show (policies embedded)
        PATCH  /policy-sets/{id}                    update
        DELETE /policy-sets/{id}                    delete
        POST   /policy-sets/{id}/actions/sync       trigger VCS sync (source=vcs only)
        POST   /policy-sets/{id}/policies           add a policy (rego validated)
        PATCH  /policies/{id}                       update a policy
        DELETE /policies/{id}                       delete a policy
    Run lifecycle (workspace read/admin):
        GET    /runs/{run_id}/policy-evaluations    list a run's evaluations
        POST   /runs/{run_id}/actions/override-policy   admin override (workspace admin)
    Runner protocol (runner token, run_id-scoped):
        GET    /runs/{run_id}/policy-bundle         applicable sets + context
        POST   /runs/{run_id}/policy-results        record evaluation outcomes
"""

import re
import uuid
from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from terrapod.api.dependencies import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
    require_runner_for_run,
)
from terrapod.db.models import (
    Policy,
    PolicyEvaluation,
    PolicySet,
    Run,
    VCSConnection,
    Workspace,
    generate_uuid7,
    now_utc,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import policy_engine, policy_set_service, run_service
from terrapod.services.workspace_rbac_service import (
    has_permission,
    resolve_workspace_permission_for,
)

router = APIRouter(tags=["policy-sets"])
logger = get_logger(__name__)

VALID_ENFORCEMENT = {"advisory", "mandatory"}
# A Terrapod policy must declare `package terrapod` — Terrapod queries
# `data.terrapod.deny`. A policy in another package, including a sub-
# package like `terrapod.aws.s3`, would silently always pass, so we
# require the package to be *exactly* `terrapod` (trailing whitespace
# or a comment is fine). `\b` alone matches at the `.` in `terrapod.foo`
# so we anchor with `\s*$` / a `#` instead.
_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+terrapod\s*(#.*)?$")

# A Terrapod policy must also actually define a `deny` rule — a policy
# in `package terrapod` that doesn't define `deny` would silently
# always pass. v1 syntax allows `deny contains msg if { ... }`,
# `deny := [...]`, or `deny = ...`. The regex matches the rule head at
# the start of a line.
_DENY_RULE_RE = re.compile(r"(?m)^\s*deny\s+(contains|:=|=)")


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _policy_json(p: Policy) -> dict:
    return {
        "id": f"pol-{p.id}",
        "type": "policies",
        "attributes": {
            "name": p.name,
            "description": p.description or "",
            "rego": p.rego,
            "created-at": _rfc3339(p.created_at),
            "updated-at": _rfc3339(p.updated_at),
        },
        "relationships": {
            "policy-set": {"data": {"id": f"polset-{p.policy_set_id}", "type": "policy-sets"}},
        },
    }


def _policy_set_json(ps: PolicySet, *, embed_policies: bool = False) -> dict:
    attrs = {
        "name": ps.name,
        "description": ps.description or "",
        "enforcement-level": ps.enforcement_level,
        "enabled": ps.enabled,
        "global-scope": ps.global_scope,
        "allow-labels": ps.allow_labels or {},
        "allow-names": ps.allow_names or [],
        "deny-labels": ps.deny_labels or {},
        "deny-names": ps.deny_names or [],
        "source": ps.source,
        "vcs-connection-id": f"vcs-{ps.vcs_connection_id}" if ps.vcs_connection_id else None,
        "vcs-repo-url": ps.vcs_repo_url or None,
        "vcs-branch": ps.vcs_branch or None,
        "policy-path": ps.policy_path or None,
        "vcs-last-commit-sha": ps.vcs_last_commit_sha or None,
        "vcs-last-synced-at": _rfc3339(ps.vcs_last_synced_at) if ps.vcs_last_synced_at else None,
        "vcs-last-error": ps.vcs_last_error,
        "policy-count": len(ps.policies),
        "created-by": ps.created_by or "",
        "created-at": _rfc3339(ps.created_at),
        "updated-at": _rfc3339(ps.updated_at),
    }
    doc: dict = {"id": f"polset-{ps.id}", "type": "policy-sets", "attributes": attrs}
    if embed_policies:
        doc["relationships"] = {
            "policies": {
                "data": [_policy_json(p) for p in sorted(ps.policies, key=lambda x: x.name)]
            }
        }
    return doc


def _evaluation_json(e: PolicyEvaluation) -> dict:
    return {
        "id": f"pe-{e.id}",
        "type": "policy-evaluations",
        "attributes": {
            "policy-set-name": e.policy_set_name,
            "enforcement-level": e.enforcement_level,
            "outcome": e.outcome,
            "result": e.result or {},
            "overridden-by": e.overridden_by,
            "overridden-at": _rfc3339(e.overridden_at) if e.overridden_at else None,
            "created-at": _rfc3339(e.created_at),
        },
        "relationships": {
            "policy-set": (
                {"data": {"id": f"polset-{e.policy_set_id}", "type": "policy-sets"}}
                if e.policy_set_id
                else {"data": None}
            ),
        },
    }


async def _get_policy_set(db: AsyncSession, ps_id: str) -> PolicySet:
    try:
        ps_uuid = uuid.UUID(ps_id.removeprefix("polset-"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Policy set not found") from exc
    ps = (
        await db.execute(
            select(PolicySet)
            .where(PolicySet.id == ps_uuid)
            .options(selectinload(PolicySet.policies))
        )
    ).scalar_one_or_none()
    if ps is None:
        raise HTTPException(status_code=404, detail="Policy set not found")
    return ps


def _validate_enforcement(level: str) -> str:
    if level not in VALID_ENFORCEMENT:
        raise HTTPException(
            status_code=422,
            detail=f"enforcement-level must be one of {sorted(VALID_ENFORCEMENT)}",
        )
    return level


# ── Policy set CRUD ───────────────────────────────────────────────────


@router.get("/policy-sets")
async def list_policy_sets(
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all policy sets."""
    rows = (
        (
            await db.execute(
                select(PolicySet).options(selectinload(PolicySet.policies)).order_by(PolicySet.name)
            )
        )
        .scalars()
        .all()
    )
    return JSONResponse(content={"data": [_policy_set_json(ps) for ps in rows]})


@router.post("/policy-sets", status_code=201)
async def create_policy_set(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a policy set."""
    attrs = body.get("data", {}).get("attributes", {})
    name = (attrs.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")

    source = attrs.get("source", "inline")
    if source not in ("inline", "vcs"):
        raise HTTPException(status_code=422, detail="source must be 'inline' or 'vcs'")

    vcs_connection_id = None
    if source == "vcs":
        vcs_conn_id_raw = attrs.get("vcs-connection-id", "")
        if not vcs_conn_id_raw:
            raise HTTPException(
                status_code=422, detail="vcs-connection-id is required for VCS policy sets"
            )
        try:
            vcs_connection_id = uuid.UUID(vcs_conn_id_raw.removeprefix("vcs-"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid vcs-connection-id") from exc
        conn = (
            await db.execute(select(VCSConnection).where(VCSConnection.id == vcs_connection_id))
        ).scalar_one_or_none()
        if conn is None:
            raise HTTPException(status_code=404, detail="VCS connection not found")
        if not attrs.get("vcs-repo-url"):
            raise HTTPException(
                status_code=422, detail="vcs-repo-url is required for VCS policy sets"
            )

    ps = PolicySet(
        id=generate_uuid7(),
        name=name,
        description=attrs.get("description", "") or "",
        enforcement_level=_validate_enforcement(attrs.get("enforcement-level", "advisory")),
        enabled=bool(attrs.get("enabled", True)),
        global_scope=bool(attrs.get("global-scope", False)),
        allow_labels=attrs.get("allow-labels", {}) or {},
        allow_names=attrs.get("allow-names", []) or [],
        deny_labels=attrs.get("deny-labels", {}) or {},
        deny_names=attrs.get("deny-names", []) or [],
        source=source,
        vcs_connection_id=vcs_connection_id,
        vcs_repo_url=attrs.get("vcs-repo-url", "") if source == "vcs" else "",
        vcs_branch=attrs.get("vcs-branch", "") if source == "vcs" else "",
        policy_path=attrs.get("policy-path", "") if source == "vcs" else "",
        created_by=user.email,
    )
    db.add(ps)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail=f"A policy set named '{name}' already exists"
        ) from exc
    ps = await _get_policy_set(db, f"polset-{ps.id}")
    return JSONResponse(
        content={"data": _policy_set_json(ps, embed_policies=True)}, status_code=201
    )


@router.get("/policy-sets/{ps_id}")
async def show_policy_set(
    ps_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a policy set with its policies embedded."""
    ps = await _get_policy_set(db, ps_id)
    return JSONResponse(content={"data": _policy_set_json(ps, embed_policies=True)})


@router.patch("/policy-sets/{ps_id}")
async def update_policy_set(
    ps_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Partial update of a policy set."""
    ps = await _get_policy_set(db, ps_id)
    attrs = body.get("data", {}).get("attributes", {})

    if "source" in attrs:
        raise HTTPException(status_code=422, detail="source is immutable after creation")

    if "name" in attrs:
        new_name = (attrs.get("name") or "").strip()
        if not new_name:
            raise HTTPException(status_code=422, detail="name cannot be empty")
        ps.name = new_name
    if "description" in attrs:
        ps.description = attrs.get("description", "") or ""
    if "enforcement-level" in attrs:
        ps.enforcement_level = _validate_enforcement(attrs["enforcement-level"])
    if "enabled" in attrs:
        ps.enabled = bool(attrs["enabled"])
    if "global-scope" in attrs:
        ps.global_scope = bool(attrs["global-scope"])
    if "allow-labels" in attrs:
        ps.allow_labels = attrs.get("allow-labels", {}) or {}
    if "allow-names" in attrs:
        ps.allow_names = attrs.get("allow-names", []) or []
    if "deny-labels" in attrs:
        ps.deny_labels = attrs.get("deny-labels", {}) or {}
    if "deny-names" in attrs:
        ps.deny_names = attrs.get("deny-names", []) or []

    # VCS config fields (only applicable when source=vcs)
    if ps.source == "vcs":
        if "vcs-repo-url" in attrs:
            ps.vcs_repo_url = attrs["vcs-repo-url"] or ""
        if "vcs-branch" in attrs:
            ps.vcs_branch = attrs["vcs-branch"] or ""
        if "policy-path" in attrs:
            ps.policy_path = attrs["policy-path"] or ""

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="A policy set with that name already exists"
        ) from exc
    ps = await _get_policy_set(db, ps_id)
    return JSONResponse(content={"data": _policy_set_json(ps, embed_policies=True)})


@router.delete("/policy-sets/{ps_id}", status_code=204)
async def delete_policy_set(
    ps_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Delete a policy set. Its policies cascade; recorded evaluations are
    kept (their policy_set_id is nulled, name snapshot retained)."""
    ps = await _get_policy_set(db, ps_id)
    await db.delete(ps)
    await db.commit()
    return JSONResponse(content=None, status_code=204)


# ── VCS Sync Action ──────────────────────────────────────────────────


@router.post("/policy-sets/{ps_id}/actions/sync", status_code=202)
async def trigger_sync_policy_set(
    ps_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Enqueue an immediate sync of a VCS-sourced policy set. Returns 202 Accepted."""
    ps = await _get_policy_set(db, ps_id)
    if ps.source != "vcs":
        raise HTTPException(status_code=409, detail="Only VCS-sourced policy sets can be synced")

    from terrapod.services.scheduler import enqueue_trigger

    await enqueue_trigger(
        "policy_vcs_sync",
        payload={"policy_set_id": str(ps.id)},
        dedup_key=f"policy_vcs_sync:{ps.id}",
        dedup_ttl=30,
    )
    return JSONResponse(
        content={"data": _policy_set_json(ps, embed_policies=True)},
        status_code=202,
    )


# ── Policy CRUD ───────────────────────────────────────────────────────


async def _validate_rego(rego: str) -> None:
    """Reject Rego that won't compile or doesn't declare package terrapod.

    Three checks, cheapest first: non-empty, exact `package terrapod`
    declaration, a `deny` rule head, then `opa check` for syntax. The
    package + deny checks catch the silent-always-pass footguns (a
    policy in the wrong package, or one that never defines a deny rule)
    that `opa check` won't flag.
    """
    if not rego or not rego.strip():
        raise HTTPException(status_code=422, detail="rego source is required")
    if not _PACKAGE_RE.search(rego):
        raise HTTPException(
            status_code=422,
            detail=(
                "policy Rego must declare 'package terrapod' exactly "
                "(sub-packages like 'package terrapod.foo' are not evaluated by Terrapod)"
            ),
        )
    if not _DENY_RULE_RE.search(rego):
        raise HTTPException(
            status_code=422,
            detail=(
                "policy Rego must define a 'deny' rule "
                "(e.g. `deny contains msg if { ... }`); "
                "a policy with no deny rule would silently always pass"
            ),
        )
    err = await policy_engine.check_rego(rego)
    if err is not None:
        raise HTTPException(status_code=422, detail=f"Rego failed to compile: {err}")


@router.post("/policy-sets/{ps_id}/policies", status_code=201)
async def add_policy(
    ps_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Add a policy to a set. The Rego is validated with `opa check`."""
    ps = await _get_policy_set(db, ps_id)
    if ps.source == "vcs":
        raise HTTPException(
            status_code=409,
            detail="Cannot add inline policies to a VCS-sourced policy set — policies are managed by the linked repository",
        )
    attrs = body.get("data", {}).get("attributes", {})
    name = (attrs.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    rego = attrs.get("rego", "") or ""
    await _validate_rego(rego)

    policy = Policy(
        policy_set_id=ps.id,
        name=name,
        description=attrs.get("description", "") or "",
        rego=rego,
    )
    db.add(policy)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Policy set already has a policy named '{name}'",
        ) from exc
    await db.refresh(policy)
    return JSONResponse(content={"data": _policy_json(policy)}, status_code=201)


async def _get_policy(db: AsyncSession, policy_id: str) -> Policy:
    try:
        p_uuid = uuid.UUID(policy_id.removeprefix("pol-"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Policy not found") from exc
    policy = (await db.execute(select(Policy).where(Policy.id == p_uuid))).scalar_one_or_none()
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    return policy


@router.patch("/policies/{policy_id}")
async def update_policy(
    policy_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Partial update of a policy. A changed Rego is re-validated."""
    policy = await _get_policy(db, policy_id)
    ps = await _get_policy_set(db, f"polset-{policy.policy_set_id}")
    if ps.source == "vcs":
        raise HTTPException(
            status_code=409,
            detail="Cannot edit policies on a VCS-sourced policy set — push changes to the repository",
        )
    attrs = body.get("data", {}).get("attributes", {})

    if "name" in attrs:
        new_name = (attrs.get("name") or "").strip()
        if not new_name:
            raise HTTPException(status_code=422, detail="name cannot be empty")
        policy.name = new_name
    if "description" in attrs:
        policy.description = attrs.get("description", "") or ""
    if "rego" in attrs:
        rego = attrs.get("rego", "") or ""
        await _validate_rego(rego)
        policy.rego = rego

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="The policy set already has a policy with that name"
        ) from exc
    await db.refresh(policy)
    return JSONResponse(content={"data": _policy_json(policy)})


@router.delete("/policies/{policy_id}", status_code=204)
async def delete_policy(
    policy_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Delete a single policy."""
    policy = await _get_policy(db, policy_id)
    ps = await _get_policy_set(db, f"polset-{policy.policy_set_id}")
    if ps.source == "vcs":
        raise HTTPException(
            status_code=409,
            detail="Cannot delete policies from a VCS-sourced policy set — remove the file from the repository",
        )
    await db.delete(policy)
    await db.commit()
    return JSONResponse(content=None, status_code=204)


# ── Run policy evaluations ────────────────────────────────────────────


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
    perm = await resolve_workspace_permission_for(db, user, ws)
    if not has_permission(perm, "read"):
        raise HTTPException(status_code=403, detail="Requires read permission on workspace")
    return run


@router.get("/runs/{run_id}/policy-evaluations")
async def list_run_policy_evaluations(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List the policy evaluations recorded for a run."""
    run = await _get_run_for_read(db, run_id, user)
    evals = await policy_set_service.get_run_evaluations(db, run.id)
    summary = await policy_set_service.run_policy_summary(db, run.id)
    return JSONResponse(
        content={
            "data": [_evaluation_json(e) for e in evals],
            "meta": {"summary": summary},
        }
    )


@router.post("/runs/{run_id}/actions/override-policy")
async def override_run_policy(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Override a run's failed policy evaluations. Requires workspace admin.

    After overriding, the run (if held in `planning` by the gate) is
    re-driven immediately rather than waiting for the next reconciler
    tick.
    """
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
    perm = await resolve_workspace_permission_for(db, user, ws)
    if not has_permission(perm, "admin"):
        raise HTTPException(status_code=403, detail="Requires admin permission on workspace")

    count = await policy_set_service.override_run_policies(db, run.id, user.email)
    await db.commit()

    # Re-drive a run still held at the post-plan policy gate.
    if run.status == "planning":
        run = await run_service.complete_plan(db, run)
        await db.commit()

    logger.info(
        "Policy evaluations overridden",
        run_id=str(run.id),
        overridden=count,
        by=user.email,
    )
    evals = await policy_set_service.get_run_evaluations(db, run.id)
    return JSONResponse(
        content={
            "data": [_evaluation_json(e) for e in evals],
            "meta": {"overridden": count, "run-status": run.status},
        }
    )


# ── Runner protocol (runner-token auth, run_id-scoped) ────────────────
#
# These two endpoints are the runner-side half of #343: the runner
# fetches the applicable policy bundle for a run, evaluates it locally
# (it already has the plan JSON on disk — no download), and POSTs the
# results back BEFORE posting plan-result. By the time complete_plan
# runs the gate query, the evaluation rows exist. This eliminates the
# JSON-wait dance, the grace timer, and the in-API OPA subprocess.
#
# Both endpoints authenticate with the runner token (stateless HMAC,
# scoped to a single run_id by `auth_method == "runner_token"`).


@router.get("/runs/{run_id}/policy-bundle")
async def get_policy_bundle(
    run_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return the applicable policy sets + Terrapod context for this run.

    Runner consumes this between `tofu show -json tfplan` and posting
    `plan-result`. Response shape is a flat JSON (not JSON:API) — the
    runner is the only consumer.

    Empty `policy_sets` means nothing in scope for this workspace; the
    runner does no evaluation and posts no results, which the API gate
    treats as PASSED.
    """
    require_runner_for_run(user, run_id)
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

    sets = await policy_set_service.applicable_policy_sets(db, ws)
    context = policy_set_service.build_run_context(ws, run)

    return JSONResponse(
        content={
            "policy_sets": [
                {
                    "id": f"polset-{ps.id}",
                    "name": ps.name,
                    "enforcement_level": ps.enforcement_level,
                    "policies": [
                        {"id": f"pol-{p.id}", "name": p.name, "rego": p.rego}
                        for p in sorted(ps.policies, key=lambda x: x.name)
                    ],
                }
                for ps in sets
            ],
            "context": context,
        }
    )


# Outcomes the runner is allowed to report. Anything else is rejected at
# the boundary so the database invariant ("outcome in passed/failed/errored")
# is enforced before the row is built.
_VALID_OUTCOMES = {"passed", "failed", "errored"}


@router.post("/runs/{run_id}/policy-results", status_code=201)
async def post_policy_results(
    run_id: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Record the runner's policy-evaluation results for this run.

    Body: ``{"results": [{policy_set_id, policy_set_name,
    enforcement_level, outcome, result}, ...]}``. Each row is persisted
    via ON CONFLICT DO NOTHING on ``(run_id, policy_set_id)``, so a
    retried POST after a transient failure is safely idempotent.
    """
    require_runner_for_run(user, run_id)
    try:
        run_uuid = uuid.UUID(run_id.removeprefix("run-"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    run = await db.get(Run, run_uuid)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    results = body.get("results")
    if not isinstance(results, list):
        raise HTTPException(status_code=422, detail="`results` must be a list of evaluation rows")

    stamp = now_utc()
    rows: list[dict] = []
    for item in results:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="each result must be an object")
        ps_id_raw = item.get("policy_set_id", "")
        try:
            ps_uuid = uuid.UUID(str(ps_id_raw).removeprefix("polset-"))
        except (ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=422, detail=f"invalid policy_set_id: {ps_id_raw!r}"
            ) from exc

        outcome = item.get("outcome", "")
        if outcome not in _VALID_OUTCOMES:
            raise HTTPException(
                status_code=422,
                detail=f"outcome must be one of {sorted(_VALID_OUTCOMES)}; got {outcome!r}",
            )

        enf = item.get("enforcement_level", "")
        if enf not in ("advisory", "mandatory"):
            raise HTTPException(
                status_code=422,
                detail=f"enforcement_level must be advisory or mandatory; got {enf!r}",
            )

        result = item.get("result", {})
        if not isinstance(result, dict):
            raise HTTPException(status_code=422, detail="result must be an object (got a non-dict)")

        ps_name = (item.get("policy_set_name") or "").strip()
        if not ps_name:
            # The name is snapshotted onto the row and shown in the UI;
            # an empty value would render as a blank set badge — a
            # contract bug on the runner side.
            raise HTTPException(
                status_code=422,
                detail="policy_set_name is required (must be a non-empty string)",
            )

        rows.append(
            {
                "id": generate_uuid7(),
                "run_id": run_uuid,
                "policy_set_id": ps_uuid,
                "policy_set_name": ps_name,
                "enforcement_level": enf,
                "outcome": outcome,
                "result": result,
                "created_at": stamp,
            }
        )

    await policy_set_service._insert_evaluations(db, rows)
    await db.commit()
    logger.info(
        "Policy results recorded by runner",
        run_id=str(run_uuid),
        sets=len(rows),
    )
    return JSONResponse(content={"recorded": len(rows)}, status_code=201)
