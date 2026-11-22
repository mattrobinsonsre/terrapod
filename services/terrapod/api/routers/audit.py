"""Audit log query endpoint (admin or audit role required).

UX CONTRACT: Audit log endpoints are consumed by the web frontend:
  - web/src/app/admin/audit-log/page.tsx (audit log query and display)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to that frontend page.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin_or_audit
from terrapod.api.pagination import build_meta
from terrapod.db.session import get_db
from terrapod.services.audit_service import query_audit_log

router = APIRouter(prefix="/admin", tags=["audit"])


def _format_timestamp(dt: datetime) -> str:
    """Format datetime as RFC3339 with Z suffix."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/audit-log")
async def list_audit_log(
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
    filter_actor: str | None = Query(None, alias="filter[actor]"),
    filter_actor_login: str | None = Query(None, alias="filter[actor-login]"),
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
        actor_login=filter_actor_login,
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
                    # The dual-actor model the schema has always carried, which no
                    # consumer could see: a vcs_user entry holds a login and an id
                    # and an empty email, so without these the UI rendered a blank
                    # actor for every PR-comment action (#2669).
                    "actor-type": entry.actor_type,
                    "actor-login": entry.actor_login,
                    "actor-id": entry.actor_id,
                    "origin": entry.origin,
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
        "meta": build_meta(total, page_number, page_size),
    }
