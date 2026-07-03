"""Execution hook resolution + validation (#619).

Hooks are a library of reusable custom-shell steps run inside the runner Job at
one of five fixed points (pre_init/pre_plan/post_plan/pre_apply/post_apply).
A hook reaches a workspace ONLY via an explicit ``ExecutionHookWorkspace``
association — there is no global flag — so this resolver is a straight
association join (mirrors ``variable_service._get_applicable_varsets`` for the
assigned case, minus the global union).

CRUD lives in ``api/routers/execution_hooks.py`` (like variable sets); this
module owns hook-point validation and the run-time resolver that feeds the
``next_run`` payload.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import (
    EXECUTION_HOOK_POINTS,
    ExecutionHook,
    ExecutionHookWorkspace,
)


def validate_hook_point(hook_point: str) -> None:
    """Reject an unknown hook point with HTTP 422 (the label chokepoint pattern).

    Called at every write path (create + update) so a bad point can never be
    stored, which would trap the row (unrunnable + confusing).
    """
    if hook_point not in EXECUTION_HOOK_POINTS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid hook_point '{hook_point}'. Must be one of: "
                f"{', '.join(EXECUTION_HOOK_POINTS)}"
            ),
        )


async def resolve_hooks_for_workspace(db: AsyncSession, workspace_id: uuid.UUID) -> list[dict]:
    """Return the enabled hooks associated with a workspace, ready for delivery.

    Ordered by ``(priority ASC, name ASC)`` so the runner runs the hooks at each
    point deterministically (the runner filters this flat list by ``hook_point``,
    preserving the order). Disabled hooks and hooks not associated with the
    workspace are excluded. Returns a plain list of dicts (runner-side field
    names, not JSON:API), embedded verbatim into the run response.
    """
    result = await db.execute(
        select(ExecutionHook)
        .join(
            ExecutionHookWorkspace,
            ExecutionHook.id == ExecutionHookWorkspace.hook_id,
        )
        .where(
            ExecutionHookWorkspace.workspace_id == workspace_id,
            ExecutionHook.enabled.is_(True),
        )
        .order_by(ExecutionHook.priority, ExecutionHook.name)
    )
    hooks = result.scalars().all()
    return [
        {
            "hook_point": h.hook_point,
            "name": h.name,
            "script": h.script,
        }
        for h in hooks
    ]
