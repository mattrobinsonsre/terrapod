"""Audit log query endpoint (admin or audit role required)."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin_or_audit
from terrapod.db.session import get_db
from terrapod.services.audit_service import query_audit_log

router = APIRouter(prefix="/api/v2/admin", tags=["audit"])


def _format_timestamp(dt: datetime) -> str:
    """Format datetime as RFC3339 with Z suffix."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/audit-log")
async def list_audit_log(
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
    filter_actor: str | None = Query(None, alias="filter[actor]"),
    filter_resource_type: str | None = Query(None, alias="filter[resource-type]"),
    filter_action: str | None = Query(None, alias="filter[action]"),
    filter_since: datetime | None = Query(None, alias="filter[since]"),
    filter_until: datetime | None = Query(None, alias="filter[until]"),
    page_number: int = Query(1, alias="page[number]", ge=1),
    page_size: int = Query(20, alias="page[size]", ge=1, le=100),
) -> dict:
    """List audit log entries with optional filters."""
    entries, total = await query_audit_log(
        db,
        actor=filter_actor,
        resource_type=filter_resource_type,
        action=filter_action,
        since=filter_since,
        until=filter_until,
        page_number=page_number,
        page_size=page_size,
    )

    return {
        "data": [
            {
                "id": str(entry.id),
                "type": "audit-log-entries",
                "attributes": {
                    "timestamp": _format_timestamp(entry.timestamp),
                    "actor-email": entry.actor_email,
                    "actor-ip": entry.actor_ip,
                    "action": entry.action,
                    "resource-type": entry.resource_type,
                    "resource-id": entry.resource_id,
                    "status-code": entry.status_code,
                    "request-id": entry.request_id,
                    "duration-ms": entry.duration_ms,
                    "detail": entry.detail,
                },
            }
            for entry in entries
        ],
        "meta": {
            "pagination": {
                "current-page": page_number,
                "page-size": page_size,
                "total-count": total,
                "total-pages": (total + page_size - 1) // page_size if total > 0 else 0,
            }
        },
    }
