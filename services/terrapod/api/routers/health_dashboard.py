"""Health dashboard endpoint — platform health at a glance.

Admin/audit only. Aggregates workspace, run, and listener health data
on demand — no background jobs or new tables needed.

UX CONTRACT: Consumed by web/src/app/admin/health/page.tsx
"""

import asyncio
import json
from datetime import UTC, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import AgentPool, Run, Workspace
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger

router = APIRouter(prefix="/api/v2", tags=["health-dashboard"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/admin/health-dashboard")
async def get_health_dashboard(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Platform health dashboard. Requires admin or audit role."""
    if "admin" not in user.roles and "audit" not in user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin or audit role required",
        )

    workspace_data = await _get_workspace_health(db)
    run_data = await _get_run_health(db)
    listener_data = await _get_listener_health(db)

    return JSONResponse(
        content={
            "data": {
                "id": "health-dashboard",
                "type": "health-dashboards",
                "attributes": {
                    "workspaces": workspace_data,
                    "runs": run_data,
                    "listeners": listener_data,
                },
            }
        }
    )


async def _get_workspace_health(db: AsyncSession) -> dict:
    """Aggregate workspace health data."""
    # Total count
    total_result = await db.execute(select(func.count()).select_from(Workspace))
    total = total_result.scalar_one()

    # Locked count
    locked_result = await db.execute(
        select(func.count()).select_from(Workspace).where(Workspace.locked.is_(True))
    )
    locked = locked_result.scalar_one()

    # Drift-enabled count
    drift_enabled_result = await db.execute(
        select(func.count())
        .select_from(Workspace)
        .where(Workspace.drift_detection_enabled.is_(True))
    )
    drift_enabled = drift_enabled_result.scalar_one()

    # Drift status breakdown
    drift_breakdown_result = await db.execute(
        select(
            Workspace.drift_status,
            func.count(),
        ).group_by(Workspace.drift_status)
    )
    by_drift_status = {"unchecked": 0, "no-drift": 0, "drifted": 0, "errored": 0}
    for drift_status, count in drift_breakdown_result.all():
        if drift_status == "":
            by_drift_status["unchecked"] += count
        elif drift_status == "no_drift":
            by_drift_status["no-drift"] += count
        elif drift_status == "drifted":
            by_drift_status["drifted"] += count
        elif drift_status == "errored":
            by_drift_status["errored"] += count

    # Stale workspaces (top 20 by days since last applied run, or never applied)
    stale = await _get_stale_workspaces(db)

    return {
        "total": total,
        "locked": locked,
        "drift-enabled": drift_enabled,
        "by-drift-status": by_drift_status,
        "stale": stale,
    }


async def _get_stale_workspaces(db: AsyncSession, limit: int = 20) -> list[dict]:
    """Get workspaces sorted by staleness (days since last applied run).

    Returns the top N most stale workspaces. Workspaces that have never
    had an applied run appear first (most stale).
    """
    # Subquery: latest applied run per workspace
    latest_apply = (
        select(
            Run.workspace_id,
            func.max(Run.apply_finished_at).label("last_applied_at"),
        )
        .where(Run.status == "applied")
        .group_by(Run.workspace_id)
        .subquery()
    )

    result = await db.execute(
        select(
            Workspace.id,
            Workspace.name,
            Workspace.drift_status,
            latest_apply.c.last_applied_at,
        )
        .outerjoin(latest_apply, Workspace.id == latest_apply.c.workspace_id)
        .order_by(
            # NULLs first (never applied = most stale)
            case((latest_apply.c.last_applied_at.is_(None), 0), else_=1),
            latest_apply.c.last_applied_at.asc(),
        )
        .limit(limit)
    )

    from datetime import datetime

    now = datetime.now(UTC)
    stale_list = []
    for ws_id, ws_name, drift_status, last_applied_at in result.all():
        if last_applied_at is not None:
            days_since = (now - last_applied_at).days
        else:
            days_since = -1  # never applied

        stale_list.append(
            {
                "id": f"ws-{ws_id}",
                "name": ws_name,
                "last-applied-at": _rfc3339(last_applied_at) if last_applied_at else "",
                "days-since-apply": days_since,
                "drift-status": drift_status or "unchecked",
            }
        )

    return stale_list


async def _get_run_health(db: AsyncSession) -> dict:
    """Aggregate run health data for the last 24 hours."""
    now_utc = func.now()
    cutoff = now_utc - timedelta(hours=24)

    # Current queue depth
    queued_result = await db.execute(
        select(func.count()).select_from(Run).where(Run.status == "queued")
    )
    queued = queued_result.scalar_one()

    # In-progress count
    in_progress_result = await db.execute(
        select(func.count()).select_from(Run).where(Run.status.in_(["planning", "applying"]))
    )
    in_progress = in_progress_result.scalar_one()

    # 24h breakdown by terminal status
    recent_result = await db.execute(
        select(
            Run.status,
            func.count(),
        )
        .where(Run.created_at >= cutoff)
        .group_by(Run.status)
    )
    recent_24h = {"total": 0, "applied": 0, "errored": 0, "canceled": 0}
    for run_status, count in recent_result.all():
        recent_24h["total"] += count
        if run_status == "applied":
            recent_24h["applied"] += count
        elif run_status == "errored":
            recent_24h["errored"] += count
        elif run_status == "canceled":
            recent_24h["canceled"] += count

    # Average plan duration (last 24h, only completed plans)
    plan_avg_result = await db.execute(
        select(
            func.avg(
                func.extract("epoch", Run.plan_finished_at)
                - func.extract("epoch", Run.plan_started_at)
            )
        ).where(
            Run.plan_started_at.isnot(None),
            Run.plan_finished_at.isnot(None),
            Run.created_at >= cutoff,
        )
    )
    avg_plan_secs = plan_avg_result.scalar_one()

    # Average apply duration (last 24h, only completed applies)
    apply_avg_result = await db.execute(
        select(
            func.avg(
                func.extract("epoch", Run.apply_finished_at)
                - func.extract("epoch", Run.apply_started_at)
            )
        ).where(
            Run.apply_started_at.isnot(None),
            Run.apply_finished_at.isnot(None),
            Run.created_at >= cutoff,
        )
    )
    avg_apply_secs = apply_avg_result.scalar_one()

    return {
        "queued": queued,
        "in-progress": in_progress,
        "recent-24h": recent_24h,
        "average-plan-duration-seconds": round(avg_plan_secs) if avg_plan_secs else 0,
        "average-apply-duration-seconds": round(avg_apply_secs) if avg_apply_secs else 0,
    }


async def _get_listener_health(db: AsyncSession) -> dict:
    """Aggregate listener health data from Redis (listeners are ephemeral).

    Gets pool IDs + names from the DB, then reads listener data from Redis
    using the pool_listeners sets and listener hashes.
    """
    from terrapod.services import agent_pool_service

    # Get all pools from DB (for pool names)
    pool_result = await db.execute(select(AgentPool))
    pools = list(pool_result.scalars().all())
    pool_names: dict[str, str] = {str(p.id): p.name for p in pools}

    details = []
    online_count = 0

    for pool in pools:
        listeners = await agent_pool_service.list_listeners(pool.id)
        for lis in listeners:
            is_online = lis.get("status") == "online"
            if is_online:
                online_count += 1

            details.append(
                {
                    "id": f"listener-{lis['id']}",
                    "name": lis.get("name", ""),
                    "pool-name": pool_names.get(lis.get("pool_id", ""), ""),
                    "status": "online" if is_online else "offline",
                    "capacity": int(lis.get("capacity", 0)),
                    "active-runs": int(lis.get("active_runs", 0)),
                    "last-heartbeat": lis.get("last_heartbeat", ""),
                }
            )

    return {
        "total": len(details),
        "online": online_count,
        "offline": len(details) - online_count,
        "details": details,
    }


# ── SSE (Server-Sent Events) ─────────────────────────────────────────────


@router.get("/admin/health-dashboard/events")
async def health_dashboard_events(
    request: Request,
) -> EventSourceResponse:
    """Stream admin health events via SSE for real-time dashboard updates.

    Uses short-lived DB session for auth, then releases before SSE streaming.
    Requires admin or audit role.
    """
    from terrapod.api.dependencies import authenticate_request
    from terrapod.redis.client import ADMIN_EVENTS_CHANNEL, subscribe_channel

    user = await authenticate_request(request)
    if "admin" not in user.roles and "audit" not in user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin or audit role required",
        )

    pubsub = await subscribe_channel(ADMIN_EVENTS_CHANNEL)

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
                        "event": payload.get("event", "update"),
                        "data": json.dumps(payload),
                    }
                else:
                    yield {"comment": "keepalive"}
                    await asyncio.sleep(1)
        finally:
            await pubsub.unsubscribe(ADMIN_EVENTS_CHANNEL)
            await pubsub.aclose()

    return EventSourceResponse(event_generator())
