"""Deprecation signalling for the public API surface (#550).

Part of the v1.0.0 stability program. When an endpoint (or a response attribute
served by one) is scheduled for removal, Terrapod must give consumers advance,
machine-readable warning **before** the breaking change lands in a future MAJOR —
never remove-without-notice. This is the runtime half of the deprecation policy in
`docs/versioning-and-support.md`; the human-facing list of what is deprecated and
when it sunsets lives in `docs/deprecations.md`.

The contract we emit on a deprecated endpoint, per the IETF drafts the ecosystem
already understands:

- ``Deprecation: true`` — the resource is deprecated (draft-ietf-httpapi-deprecation-header).
- ``Sunset: <IMF-fixdate>`` — the date at/after which it may stop working (RFC 8594).
- ``Link: <url>; rel="deprecation"; type="text/html"`` — where to read what to do instead.

Call ``mark_deprecated(response, ...)`` from any handler being wound down. It only
*adds* headers — it never changes the response body or status — so a lagging client
keeps working through the whole deprecation window (>= 2 minor releases) and simply
sees the warning. Removal happens only in a MAJOR, after the ``Sunset`` date has
passed and the window is complete.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from email.utils import format_datetime

from fastapi import Response

# Canonical docs page listing every active deprecation and its sunset date.
DEPRECATIONS_DOC_URL = "https://github.com/mattrobinsonsre/terrapod/blob/main/docs/deprecations.md"


def _imf_fixdate(sunset: date) -> str:
    """RFC 7231 IMF-fixdate (e.g. ``Wed, 01 Jan 2027 00:00:00 GMT``) for a date.

    ``Sunset`` (RFC 8594) is an HTTP-date; midnight UTC of the given day is the
    conventional encoding for a whole-day sunset.
    """
    dt = datetime(sunset.year, sunset.month, sunset.day, tzinfo=UTC)
    return format_datetime(dt, usegmt=True)


def mark_deprecated(
    response: Response,
    *,
    sunset: date,
    link: str = DEPRECATIONS_DOC_URL,
) -> None:
    """Attach ``Deprecation`` / ``Sunset`` / ``Link`` headers to a response.

    Args:
        response: the FastAPI ``Response`` the handler will return (inject it as a
            handler parameter — FastAPI populates it).
        sunset: the date on/after which the endpoint may stop working. MUST be at
            least two minor releases out per the deprecation policy.
        link: URL explaining the deprecation and the replacement; defaults to the
            project deprecations page.
    """
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = _imf_fixdate(sunset)
    response.headers["Link"] = f'<{link}>; rel="deprecation"; type="text/html"'
