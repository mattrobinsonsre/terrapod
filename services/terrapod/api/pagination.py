"""Shared JSON:API pagination for list endpoints.

The Terrapod convention (see AGENTS.md → "List endpoints — optional
pagination"): every list endpoint accepts optional ``page[size]`` +
``page[number]`` (the TFE / JSON:API wire shape, so `go-tfe` / go-terrapod
clients paginate normally) and always returns a ``meta.pagination`` block.

**Non-paged** — signalled by an explicit ``page[size]=0`` **or** the absence of
any paging params — returns the **full result set** as a single page. This keeps
bulk-fetch clients (and the web UI, which fetches the whole list and filters
client-side) working unchanged, so adding pagination is an additive,
backward-compatible change rather than a breaking one. **Paged** — ``page[size]``
>= 1 — returns that page (capped at ``MAX_PAGE_SIZE``).

The meta shape matches the already-paginated endpoints (audit/users/runs) and
what go-terrapod's ``parseListMeta`` decodes: ``current-page``, ``page-size``,
``total-count``, ``total-pages``.

For RBAC-filtered lists, slice **after** the permission filter (pass the visible
list here) so pages are full and counts are correct.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

# Upper bound on an explicitly-requested page size, to cap the work a single
# paged request can ask for. Does not apply to the non-paged (absent-params)
# path, which returns the caller's full result set by design.
MAX_PAGE_SIZE = 100


def parse_page_params(request: Request | None) -> tuple[int, int | None]:
    """Return ``(page_number, page_size)`` from JSON:API ``page[*]`` query params.

    ``page_size`` is ``None`` when the client requested no pagination (so the
    caller should return the full list). ``page_number`` defaults to 1.
    """

    def _int(key: str) -> int | None:
        raw = request.query_params.get(key) if request is not None else None
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    size = _int("page[size]")
    if size is not None and size < 1:
        size = None
    number = _int("page[number]") or 1
    if number < 1:
        number = 1
    return number, size


def paginate(items: list[Any], request: Request | None) -> tuple[list[Any], dict]:
    """Apply optional JSON:API pagination to an already-materialised list.

    - ``page[size]`` present → return that page (size capped at ``MAX_PAGE_SIZE``);
      a ``page[number]`` past the end yields an empty page (not an error).
    - ``page[size]`` absent → return the whole list as a single page.

    Returns ``(page_items, meta)`` where ``meta`` is ``{"pagination": {...}}``.
    """
    total = len(items)
    number, size = parse_page_params(request)

    if size is None:
        page_items = items
        page_size = total or 1
        page_number = 1
        total_pages = 1
    else:
        size = min(size, MAX_PAGE_SIZE)
        start = (number - 1) * size
        page_items = items[start : start + size]
        page_size = size
        page_number = number
        total_pages = (total + size - 1) // size if total > 0 else 0

    meta = {
        "pagination": {
            "current-page": page_number,
            "page-size": page_size,
            "total-count": total,
            "total-pages": total_pages,
        }
    }
    return page_items, meta
