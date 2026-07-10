"""The frozen API route contract (#550).

Terrapod's route surface is a stability guarantee for consumers that can lag the
server across version skew: the `terraform`/`tofu` `cloud` backend + `go-tfe`
clients on `/api/v2/`, and — crucially — the runner + listener wire protocol on
`/api/terrapod/v1/` (runners and listeners live in remote clusters and may be
several minor versions behind the API during a rolling upgrade). Removing or
renaming a route breaks those consumers.

`tests/api/test_route_contract.py` pins the full route set against a committed
snapshot and fails CI on any removal/rename. This module is the single
extraction point that both the test and the snapshot regenerator import, so the
two can never drift.
"""

from __future__ import annotations

from fastapi import FastAPI

# Interactive-docs + schema routes are FastAPI internals, not part of the API
# contract — their presence/absence is not a compatibility concern.
_NON_CONTRACT_PATHS = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

# HEAD/OPTIONS are auto-added by Starlette for GET routes and CORS; they carry no
# independent contract.
_IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})


def route_signatures(app: FastAPI) -> list[str]:
    """Return the app's route contract as a sorted list of ``"METHOD /path"``.

    Deterministic and stable: the templated path (e.g.
    ``/api/v2/workspaces/{workspace_id}``) is used, so the snapshot is
    order-independent and unaffected by request data.
    """
    sigs: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or path in _NON_CONTRACT_PATHS:
            continue
        for method in methods:
            if method not in _IGNORED_METHODS:
                sigs.add(f"{method} {path}")
    return sorted(sigs)


def diff_route_contract(snapshot: list[str], current: list[str]) -> tuple[list[str], list[str]]:
    """Compare a committed snapshot against the current route set.

    Returns ``(removed, added)`` — ``removed`` are signatures in the snapshot but
    no longer served (a **breaking** change for a lagging consumer); ``added``
    are new signatures (additive, safe). Both sorted for stable output.
    """
    snap, cur = set(snapshot), set(current)
    return sorted(snap - cur), sorted(cur - snap)
