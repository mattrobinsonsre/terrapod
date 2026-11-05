#!/usr/bin/env python3
"""A catalogued CLI route is served, and a route it marks NOT SERVED is not (#2378).

`docs/tfe-cli-surface.md` is what decides whether an endpoint belongs on the
TFE-compatibility surface, and AGENTS.md makes it the authority: "if a route is
on that list it stays at `/api/tfe/v2/`; otherwise it goes under `/api/v1/`".
Nothing held it to the server.

That is how #2378 happened. Both organization run-queue routes were catalogued
as in-scope, citing exact OpenTofu call sites, and **neither was served** -- and
a 404 from either is fatal upstream, not cosmetic: `waitForRun` does
`return r, generalError("Failed to retrieve queue", err)`, so the plan or apply
aborts. The route contract could not see it (it pins the routes that exist, not
the ones that were promised), and `check_cli_surface_covered.py` could not
either (it asks whether each catalogue SECTION is exercised by the CLI-contract
suite, which is a different question).

Three directions, because each catches a different way the two drift apart:

1. **A catalogued row must be served.** The #2378 defect.
2. **A row marked `NOT SERVED` must NOT be served.** The marker is a claim like
   any other, and this is the direction that goes stale silently: once the route
   lands, a leftover marker tells the next reader a working endpoint is missing.
   #2381's by-name workspace delete is a deliberate, permanent entry here.
3. **Every route `tfe_v2.py` defines must be catalogued**, either way. That file
   is the CLI-contract router -- AGENTS.md: "don't put anything else in it" -- so
   a route in it that nobody catalogued is surface nobody decided to promise.
   Measured at 22 of 22 catalogued with no exemptions, which is why there is no
   ledger here: an exemption list is the thing that would drift next.

STDLIB ONLY, and it reads the route contract rather than importing the app: a
gate in `scripts/docs-audit/` runs against a full checkout with no Terrapod
dependencies installed, and importing `terrapod.api.app` would pull in
SQLAlchemy and the rest of the API's closure.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "tfe-cli-surface.md"
CONTRACT = ROOT / "services" / "tests" / "api" / "api_route_contract.json"
ROUTER = ROOT / "services" / "terrapod" / "api" / "routers" / "tfe_v2.py"

#: Floors, so a matcher that stops matching FAILS instead of passing on nothing.
#: Each is comfortably below the measured count (68 catalogued, 1034 served, 22
#: own routes) and above zero -- the point is to catch a broken regex, not to
#: pin a number that grows.
MIN_CATALOGUED = 40
MIN_SERVED = 300
MIN_OWN_ROUTES = 12

#: `| GET | `/path` | go-tfe method | caller |`
ROW = re.compile(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`\s*\|(.*)$", re.M)
#: `@router.get("/organizations/default/capacity")`
DECORATOR = re.compile(r'@router\.(get|post|put|patch|delete)\(\s*"([^"]+)"')
#: A path parameter's NAME differs between the two sources by design: the
#: catalogue writes `{id}` for a reader, the contract writes `{workspace_id}`
#: from the handler's own signature. Compare shape, not spelling.
PLACEHOLDER = re.compile(r"\{[^}]*\}")

TFE_PREFIX = "/api/tfe/v2"


def _normalise(method: str, path: str) -> str:
    """One comparable key from either source.

    The query half is dropped from BOTH: the catalogue writes it inside the
    backticks (`/runs/{id}?include=task_stages`) to show what the CLI asks for,
    and the contract writes it after a space (`GET /path ?filter_x,page_size`).
    Neither is part of the route's identity, and keeping it on one side only
    reported a served route as missing -- which it did on the first run here.
    """
    path = path.split("?", 1)[0].rstrip()
    return f"{method.upper()} {PLACEHOLDER.sub('{}', path)}"


def _catalogue() -> tuple[set[str], set[str]]:
    """`(expected_served, declared_unserved)` from the catalogue's own rows."""
    expected: set[str] = set()
    unserved: set[str] = set()
    for method, path, rest in ROW.findall(DOC.read_text()):
        key = _normalise(method, path)
        (unserved if "NOT SERVED" in rest.upper() else expected).add(key)
    return expected, unserved


def _served() -> set[str]:
    raw = json.loads(CONTRACT.read_text())
    entries = raw if isinstance(raw, list) else raw.get("routes") or next(iter(raw.values()))
    out: set[str] = set()
    for entry in entries:
        head = str(entry).split(" ?", 1)[0].strip()
        method, _, path = head.partition(" ")
        if path:
            out.add(_normalise(method, path))
    return out


def _own_routes() -> set[str]:
    return {
        _normalise(method, TFE_PREFIX + path)
        for method, path in DECORATOR.findall(ROUTER.read_text())
    }


def main() -> int:
    problems: list[str] = []
    expected, unserved = _catalogue()
    served = _served()
    own = _own_routes()

    if len(expected) < MIN_CATALOGUED:
        problems.append(
            f"only {len(expected)} catalogued routes matched, below the floor of "
            f"{MIN_CATALOGUED}. The row matcher has stopped matching -- fix it rather "
            "than lowering this."
        )
    if len(served) < MIN_SERVED:
        problems.append(
            f"only {len(served)} served routes read from {CONTRACT.name}, below the "
            f"floor of {MIN_SERVED}. The contract reader has stopped reading."
        )
    if len(own) < MIN_OWN_ROUTES:
        problems.append(
            f"only {len(own)} routes matched in {ROUTER.name}, below the floor of "
            f"{MIN_OWN_ROUTES}. The decorator matcher has stopped matching."
        )

    for key in sorted(expected - served):
        problems.append(
            f"CATALOGUED BUT NOT SERVED: {key}\n"
            "      The catalogue promises this to `terraform`/`tofu`/`tfci`, and a 404 "
            "from a route the CLI calls is fatal upstream rather than cosmetic. Serve "
            "it, or mark the row **NOT SERVED** with the reason."
        )

    for key in sorted(unserved & served):
        problems.append(
            f"MARKED NOT SERVED BUT IS SERVED: {key}\n"
            "      The marker now tells a reader a working endpoint is missing. Remove "
            "it from the row."
        )

    for key in sorted(own - expected - unserved):
        problems.append(
            f"SERVED IN {ROUTER.name} BUT NOT CATALOGUED: {key}\n"
            "      That file is the CLI-contract router, so a route in it is surface we "
            "promise. Add the row, or move the route to a native router."
        )

    if problems:
        print(f"{DOC.name} and the server disagree:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(
        f"OK — {len(expected)} catalogued routes all served, "
        f"{len(unserved)} marked NOT SERVED and none of them is, "
        f"{len(own)} routes in {ROUTER.name} all catalogued."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
