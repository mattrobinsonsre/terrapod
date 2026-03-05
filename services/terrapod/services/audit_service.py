"""Audit logging service.

Provides helpers for logging API requests and querying the audit log.
Path parsing extracts resource_type and resource_id from TFE V2 URL patterns.
"""

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import AuditLog, generate_uuid7
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Paths to exclude from audit logging (high-frequency, low-value).
_EXCLUDED_PREFIXES = (
    "/health",
    "/ready",
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
)

# Pattern: /api/v2/{resource_type}/{resource_id}/...
_RESOURCE_PATTERN = re.compile(r"^/api/v2/(?:organizations/[^/]+/)?([a-z_-]+?)(?:/([^/]+))?(?:/|$)")


def should_audit(path: str) -> bool:
    """Return True if this path should be audited."""
    return not path.startswith(_EXCLUDED_PREFIXES)


def parse_resource(path: str) -> tuple[str, str]:
    """Extract (resource_type, resource_id) from a request path.

    Examples:
        /api/v2/workspaces/ws-abc123 → ("workspaces", "ws-abc123")
        /api/v2/organizations/default/workspaces → ("workspaces", "")
        /api/v2/admin/audit-log → ("audit-log", "")
        /oauth/authorize → ("oauth", "")
    """
    m = _RESOURCE_PATTERN.match(path)
    if m:
        return m.group(1), m.group(2) or ""
    # Fallback: first path segment after leading slash
    parts = path.strip("/").split("/")
    return parts[0] if parts else "", ""


async def log_audit_event(
    db: AsyncSession,
    *,
    actor_email: str = "",
    actor_ip: str = "",
    action: str,
    resource_type: str = "",
    resource_id: str = "",
    status_code: int,
    request_id: str = "",
    duration_ms: int = 0,
    detail: str = "",
) -> None:
    """Insert an audit log entry."""
    entry = AuditLog(
        id=generate_uuid7(),
        actor_email=actor_email,
        actor_ip=actor_ip,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        status_code=status_code,
        request_id=request_id,
        duration_ms=duration_ms,
        detail=detail,
    )
    db.add(entry)
    await db.commit()


async def purge_old_entries(db: AsyncSession, retention_days: int) -> int:
    """Delete audit log entries older than retention_days. Returns count deleted."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    result = await db.execute(delete(AuditLog).where(AuditLog.timestamp < cutoff))
    await db.commit()
    deleted = result.rowcount  # type: ignore[union-attr]
    if deleted:
        logger.info("Purged old audit log entries", count=deleted, retention_days=retention_days)
    return deleted


async def query_audit_log(
    db: AsyncSession,
    *,
    actor: str | None = None,
    resource_type: str | None = None,
    action: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page_number: int = 1,
    page_size: int = 20,
) -> tuple[list[AuditLog], int]:
    """Query audit logs with optional filters. Returns (entries, total_count)."""
    stmt = select(AuditLog)
    count_stmt = select(AuditLog.id)

    if actor:
        stmt = stmt.where(AuditLog.actor_email == actor)
        count_stmt = count_stmt.where(AuditLog.actor_email == actor)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
        count_stmt = count_stmt.where(AuditLog.resource_type == resource_type)
    if action:
        stmt = stmt.where(AuditLog.action == action)
        count_stmt = count_stmt.where(AuditLog.action == action)
    if since:
        stmt = stmt.where(AuditLog.timestamp >= since)
        count_stmt = count_stmt.where(AuditLog.timestamp >= since)
    if until:
        stmt = stmt.where(AuditLog.timestamp <= until)
        count_stmt = count_stmt.where(AuditLog.timestamp <= until)

    # Count
    count_result = await db.execute(count_stmt)
    total = len(count_result.all())

    # Page
    offset = (page_number - 1) * page_size
    stmt = stmt.order_by(AuditLog.timestamp.desc()).offset(offset).limit(page_size)

    result = await db.execute(stmt)
    entries = list(result.scalars().all())

    return entries, total
