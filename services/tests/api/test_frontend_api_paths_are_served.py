"""The bare workspace resource is NOT on the native prefix on this release line.

`GET` and `PATCH` of a workspace are served at ``/api/v2/workspaces/{id}``; only
`DELETE` is native at ``/api/terrapod/v1/workspaces/{id}``. The split is per-ROUTE,
not per-resource, so copying a neighbouring call's prefix proves nothing — and a
wrong prefix fails SILENTLY, as a 404 that leaves a control looking unsaved.

This exact mistake has shipped twice. #1777 shipped a settings control whose PATCH
404'd, and this release repeated it by rewriting a carried ``/api/v1`` path to the
native prefix across the board, including the one route that has to stay on
``/api/v2``. Both times the Python suite was green and only a browser noticed.

Deliberately narrow. A general "every path the frontend calls is served" sweep was
tried first and drowned in false positives from pre-existing code — glob patterns,
trailing-slash proxy probes, query strings built by expression, paths quoted in
prose. A gate whose allowlist is mostly false positives teaches people to edit the
allowlist, so this pins the one route pair that keeps being got wrong.
"""

from __future__ import annotations

import pathlib
import re

import pytest

HERE = pathlib.Path(__file__).resolve()

#: A call to the bare workspace resource on the NATIVE prefix. The trailing group
#: refuses a sub-resource — `/api/terrapod/v1/workspaces/{id}/vcs-refs` and friends
#: are legitimately native, and only the bare resource is the trap.
BARE_NATIVE = re.compile(
    r"/api/terrapod/v1/workspaces/(?:\$\{[^}]*\}|\$[A-Za-z_]+|\{[^}]*\})(?![/\w-])"
)


def _root() -> pathlib.Path | None:
    """The checkout root, or None in an image that ships no `web/`."""
    for cand in HERE.parents:
        if (cand / "web" / "src").is_dir() and (cand / "services").is_dir():
            return cand
    return None


ROOT = _root()


def _sources() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for sub in ("web/src", "e2e/tests", "e2e/helpers"):
        d = ROOT / sub
        if d.is_dir():
            out += [p for p in d.rglob("*") if p.suffix in (".ts", ".tsx")]
    return out


def test_nothing_calls_the_bare_workspace_resource_on_the_native_prefix() -> None:
    if ROOT is None:
        pytest.skip("web/ is not shipped in this image")

    offenders: list[str] = []
    for f in _sources():
        lines = f.read_text().splitlines()
        for n, line in enumerate(lines, 1):
            if not BARE_NATIVE.search(line):
                continue
            stripped = line.strip()
            if stripped.startswith(("//", "*", "/*")):
                continue  # prose, including the comment explaining this rule
            # DELETE is the one native verb on this resource, so it is correct
            # here. The method may sit on the same line or in the options object
            # a line or two below, which is why this looks ahead rather than
            # matching the call line alone — and it is matched case-insensitively
            # because the verb arrives in three casings: `method: "DELETE"`,
            # `page.request.delete(...)`, and a `wsDeleteUrl`-style binding whose
            # own name is the only nearby evidence of the verb.
            window = " ".join(lines[n - 1 : n + 2]).lower()
            if "delete" in window:
                continue
            offenders.append(f"{f.relative_to(ROOT)}:{n}  {stripped[:100]}")

    assert not offenders, (
        "these call the bare workspace resource at /api/terrapod/v1, which this "
        "release line does not serve — the request 404s silently and the control "
        "simply appears not to save:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse /api/v2/workspaces/{id} for GET and PATCH. DELETE is the one "
        "native verb, which is what makes a neighbour's prefix misleading. The "
        "authority is services/tests/api/api_route_contract.json."
    )


def test_the_route_contract_still_agrees_with_that_claim() -> None:
    """Otherwise this test outlives the split it exists to describe.

    If a future release starts serving the bare resource natively, the assertion
    above becomes wrong rather than merely unnecessary — so it is pinned to the
    contract instead of to a comment.
    """
    if ROOT is None:
        pytest.skip("web/ is not shipped in this image")
    import json

    routes = set(json.loads((ROOT / "services/tests/api/api_route_contract.json").read_text()))
    assert "PATCH /api/v2/workspaces/{workspace_id}" in routes
    assert "GET /api/v2/workspaces/{workspace_id}" in routes
    assert "PATCH /api/terrapod/v1/workspaces/{workspace_id}" not in routes, (
        "the bare resource is now served natively too, so the rule above is stale"
    )
    assert "DELETE /api/terrapod/v1/workspaces/{workspace_id}" in routes, (
        "DELETE is no longer native, so the asymmetry this test describes has moved"
    )
