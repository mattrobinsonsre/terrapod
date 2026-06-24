"""Shared JSON:API serialization helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def rfc3339(dt: datetime | None) -> str | None:
    """Serialize a tz-aware UTC datetime as RFC3339 with a trailing ``Z``
    (never ``+00:00``).

    Rule 10 / go-tfe compatibility: ``datetime.isoformat()`` on a tz-aware UTC
    column emits ``...+00:00``, which `go-tfe` rejects. This is the one canonical
    serializer — prefer it over per-router ``_rfc3339`` copies that have drifted
    (some used bare ``.isoformat()`` and regressed the ``Z`` suffix).
    """
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
