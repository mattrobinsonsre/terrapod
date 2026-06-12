"""Bulk workspace operations (#318) — Terrapod-native admin surface.

Two endpoints:
  POST /api/terrapod/v1/workspaces/actions/search
       — server-side workspace selection (the canonical structured filter).
  POST /api/terrapod/v1/workspaces/actions/bulk-update
       — apply a settings/run-task/notification change across every
         matched workspace in a single all-or-nothing transaction.

Hard guarantees:
  * Validation happens ONCE up front — an invalid `update` returns 422
    with zero mutation.
  * All-or-nothing: the whole batch commits, or nothing does. `dry_run`
    runs the exact same code path and `rollback()`s instead of
    `commit()`, so the preview is provably what apply would do.
  * Bulk-update triggers NO runs — it is a pure config write; the change
    lands on each workspace's next normal run.
  * Reversible by construction (it only writes settings rows); operators
    are trusted to choose the match set (explicit `all: true` allowed).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, effective_platform_roles, require_admin
from terrapod.api.labels import validate_labels
from terrapod.db.models import (
    AgentPool,
    AuditLog,
    NotificationConfiguration,
    RunTask,
    Workspace,
    generate_uuid7,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import pool_rbac_service
from terrapod.services.notification_service import VALID_TRIGGERS
from terrapod.services.run_task_service import VALID_ENFORCEMENT_LEVELS, VALID_STAGES
from terrapod.services.workspace_search_service import (
    WorkspaceFilterError,
    build_workspace_query,
    parse_filter,
)

router = APIRouter(tags=["workspace-bulk"])
logger = get_logger(__name__)

_VALID_BACKENDS = frozenset({"terraform", "tofu"})
_VALID_MODES = frozenset({"local", "agent"})
_VALID_DEST_TYPES = frozenset({"generic", "slack", "email"})

# Workspace column fields the bulk update may set, mapped to their ORM attr.
_FIELD_MAP: dict[str, str] = {
    "terraform-version": "terraform_version",
    "execution-backend": "execution_backend",
    "execution-mode": "execution_mode",
    "auto-apply": "auto_apply",
    "agent-pool-id": "agent_pool_id",
    "resource-cpu": "resource_cpu",
    "resource-memory": "resource_memory",
    "var-files": "var_files",
    "labels": "labels",
}


def _ws_summary(ws: Workspace) -> dict[str, Any]:
    return {
        "id": f"ws-{ws.id}",
        "name": ws.name,
        "execution-mode": ws.execution_mode,
        "execution-backend": ws.execution_backend,
        "terraform-version": ws.terraform_version,
        "agent-pool-id": f"apool-{ws.agent_pool_id}" if ws.agent_pool_id else None,
        "labels": ws.labels or {},
    }


def validate_run_task_specs(items: Any) -> list[dict]:
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="run-tasks must be a list")
    out: list[dict] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise HTTPException(status_code=422, detail=f"run-tasks[{i}] must be an object")
        name = str(it.get("name", "")).strip()
        url = str(it.get("url", "")).strip()
        if not name:
            raise HTTPException(status_code=422, detail=f"run-tasks[{i}].name is required")
        if not url:
            raise HTTPException(status_code=422, detail=f"run-tasks[{i}].url is required")
        stage = it.get("stage", "")
        if stage not in VALID_STAGES:
            raise HTTPException(
                status_code=422,
                detail=f"run-tasks[{i}].stage must be one of: {', '.join(sorted(VALID_STAGES))}",
            )
        enf = it.get("enforcement-level", "mandatory")
        if enf not in VALID_ENFORCEMENT_LEVELS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"run-tasks[{i}].enforcement-level must be one of: "
                    f"{', '.join(sorted(VALID_ENFORCEMENT_LEVELS))}"
                ),
            )
        out.append(
            {
                "name": name,
                "url": url,
                "hmac_key": (it.get("hmac-key") or None),
                "stage": stage,
                "enforcement_level": enf,
                "enabled": bool(it.get("enabled", True)),
            }
        )
    return out


def validate_notification_specs(items: Any) -> list[dict]:
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="notification-configurations must be a list")
    out: list[dict] = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise HTTPException(
                status_code=422,
                detail=f"notification-configurations[{i}] must be an object",
            )
        name = str(it.get("name", "")).strip()
        if not name:
            raise HTTPException(
                status_code=422,
                detail=f"notification-configurations[{i}].name is required",
            )
        dest = it.get("destination-type", "")
        if dest not in _VALID_DEST_TYPES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"notification-configurations[{i}].destination-type must be one of: "
                    f"{', '.join(sorted(_VALID_DEST_TYPES))}"
                ),
            )
        triggers = it.get("triggers", [])
        if not isinstance(triggers, list):
            raise HTTPException(
                status_code=422,
                detail=f"notification-configurations[{i}].triggers must be a list",
            )
        bad = set(triggers) - VALID_TRIGGERS
        if bad:
            raise HTTPException(
                status_code=422,
                detail=f"notification-configurations[{i}] invalid triggers: {', '.join(sorted(bad))}",
            )
        out.append(
            {
                "name": name,
                "destination_type": dest,
                "url": str(it.get("url", "")),
                "token": (it.get("token") or None),
                "triggers": triggers,
                "email_addresses": it.get("email-addresses", []),
                "enabled": bool(it.get("enabled", True)),
            }
        )
    return out


async def _validate_update(
    update: dict, db: AsyncSession, user: AuthenticatedUser
) -> dict[str, Any]:
    """Validate the homogeneous `update` payload ONCE (it is applied
    identically to every matched workspace). Raises HTTP 422 with zero
    mutation on any problem. Returns a normalised plan.
    """
    if not isinstance(update, dict) or not update:
        raise HTTPException(status_code=422, detail="'update' must be a non-empty object")

    fields: dict[str, Any] = {}
    for key, attr in _FIELD_MAP.items():
        if key not in update:
            continue
        val = update[key]
        if key == "execution-backend" and val not in _VALID_BACKENDS:
            raise HTTPException(
                status_code=422, detail="execution-backend must be 'terraform' or 'tofu'"
            )
        if key == "execution-mode" and val not in _VALID_MODES:
            raise HTTPException(status_code=422, detail="execution-mode must be 'local' or 'agent'")
        if key == "terraform-version" and not str(val).strip():
            raise HTTPException(status_code=422, detail="terraform-version cannot be empty")
        if key == "labels":
            val = validate_labels(val)  # reserved-key chokepoint (#316) → 422
        if key == "auto-apply":
            val = bool(val)
        if key == "var-files":
            if not isinstance(val, list):
                raise HTTPException(status_code=422, detail="var-files must be a list")
        if key == "agent-pool-id":
            if val in (None, ""):
                val = None
            else:
                try:
                    pid = uuid.UUID(str(val).removeprefix("apool-"))
                except ValueError as e:
                    raise HTTPException(
                        status_code=422, detail="agent-pool-id is not a UUID"
                    ) from e
                pool = await db.get(AgentPool, pid)
                if pool is None:
                    raise HTTPException(status_code=422, detail="agent-pool-id not found")
                # Assigning a pool requires pool `write` (platform admin bypass) —
                # mirrors the single-workspace rule.
                if "admin" not in effective_platform_roles(user):
                    perm = await pool_rbac_service.resolve_pool_permission(
                        db, user.email, user.roles, pool.name, pool.labels, pool.owner_email
                    )
                    if not pool_rbac_service.has_pool_permission(perm, "write"):
                        raise HTTPException(
                            status_code=403,
                            detail=f"write permission on agent pool '{pool.name}' is required",
                        )
                val = pid
        fields[attr] = val

    run_tasks = validate_run_task_specs(update["run-tasks"]) if "run-tasks" in update else None
    notifications = (
        validate_notification_specs(update["notification-configurations"])
        if "notification-configurations" in update
        else None
    )

    if not fields and run_tasks is None and notifications is None:
        raise HTTPException(
            status_code=422,
            detail="'update' had no recognised keys to apply",
        )
    return {"fields": fields, "run_tasks": run_tasks, "notifications": notifications}


@router.post("/workspaces/actions/search")
async def search_workspaces(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Resolve a structured filter to the matching workspaces. Admin only.
    No side effects — this is the discovery half of the bulk workflow.
    """
    try:
        wf = parse_filter(body.get("filter"))
        query = build_workspace_query(wf)
    except WorkspaceFilterError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    rows = (await db.execute(query)).scalars().all()
    return JSONResponse(
        content={"matched": len(rows), "workspaces": [_ws_summary(w) for w in rows]}
    )


def _diff_for(ws: Workspace, plan: dict[str, Any]) -> dict[str, Any]:
    """Compute the per-workspace change set without mutating."""
    diff: dict[str, Any] = {}
    for attr, new in plan["fields"].items():
        old = getattr(ws, attr)
        if old != new:
            diff[attr] = {"from": _jsonable(old), "to": _jsonable(new)}
    if plan["run_tasks"]:
        diff["run-tasks"] = [rt["name"] for rt in plan["run_tasks"]]
    if plan["notifications"]:
        diff["notification-configurations"] = [n["name"] for n in plan["notifications"]]
    return diff


def _jsonable(v: Any) -> Any:
    return f"apool-{v}" if isinstance(v, uuid.UUID) else v


async def _apply(
    db: AsyncSession,
    workspaces: list[Workspace],
    plan: dict[str, Any],
    actor: str,
) -> tuple[list[dict], list[dict]]:
    """Apply the plan to every workspace. Caller owns the transaction
    (commit for apply, rollback for dry-run). Run-tasks/notifications
    upsert by (workspace_id, name).
    """
    changed: list[dict] = []
    unchanged: list[dict] = []
    for ws in workspaces:
        diff = _diff_for(ws, plan)

        for attr, new in plan["fields"].items():
            setattr(ws, attr, new)

        for spec in plan["run_tasks"] or []:
            existing = (
                await db.execute(
                    select(RunTask).where(
                        RunTask.workspace_id == ws.id, RunTask.name == spec["name"]
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(RunTask(workspace_id=ws.id, **spec))
            else:
                for k, v in spec.items():
                    setattr(existing, k, v)

        for spec in plan["notifications"] or []:
            existing = (
                await db.execute(
                    select(NotificationConfiguration).where(
                        NotificationConfiguration.workspace_id == ws.id,
                        NotificationConfiguration.name == spec["name"],
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(NotificationConfiguration(workspace_id=ws.id, **spec))
            else:
                for k, v in spec.items():
                    setattr(existing, k, v)

        if diff:
            changed.append({"id": f"ws-{ws.id}", "name": ws.name, "diff": diff})
            # Audit row is added INTO the same transaction (no commit) —
            # NOT via audit_service.log_audit_event, which commits
            # internally and would break both the all-or-nothing apply
            # and the dry-run "rollback, zero side-effects" guarantee.
            db.add(
                AuditLog(
                    id=generate_uuid7(),
                    actor_email=actor,
                    action="workspace.bulk_update",
                    resource_type="workspace",
                    resource_id=f"ws-{ws.id}",
                    status_code=200,
                    detail=f"bulk-update: {sorted(diff.keys())}",
                )
            )
        else:
            unchanged.append({"id": f"ws-{ws.id}", "name": ws.name})
    return changed, unchanged


@router.post("/workspaces/actions/bulk-update")
async def bulk_update_workspaces(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Apply `update` to every workspace matching `filter`, atomically.

    `dry_run` (default true) runs the identical code path and rolls back,
    so the preview is exactly what an apply would change. Admin only.
    """
    dry_run = bool(body.get("dry_run", True))
    try:
        wf = parse_filter(body.get("filter"))
        query = build_workspace_query(wf)
    except WorkspaceFilterError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    # Up-front validation — 422 here means zero mutation.
    plan = await _validate_update(body.get("update", {}), db, user)

    workspaces = list((await db.execute(query)).scalars().all())

    try:
        changed, unchanged = await _apply(db, workspaces, plan, user.email)
        if dry_run:
            await db.rollback()
            return JSONResponse(
                content={
                    "dry_run": True,
                    "matched": len(workspaces),
                    "would_change": changed,
                    "unchanged": unchanged,
                }
            )
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        logger.warning("bulk-update failed; rolled back", error=repr(e))
        raise HTTPException(
            status_code=409,
            detail=f"bulk-update failed and was rolled back (nothing changed): {e}",
        ) from e

    logger.info(
        "bulk-update applied",
        matched=len(workspaces),
        changed=len(changed),
        actor=user.email,
    )
    return JSONResponse(
        content={
            "dry_run": False,
            "matched": len(workspaces),
            "applied": len(changed),
            "changes": changed,
            "unchanged": unchanged,
            "errors": [],
        }
    )
