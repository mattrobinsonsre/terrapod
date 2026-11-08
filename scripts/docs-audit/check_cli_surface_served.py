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

The three floors below prove the gate's SUBJECT is non-empty and say nothing
about whether its PREDICATE discriminates (#2425 §2.7). A row matcher that
quietly stops reading one spelling still clears `MIN_CATALOGUED` on the other
sixty-odd rows, and a matcher that was always too narrow clears it from the day
it was written -- which is how `.attributes."x"` went unread in the sibling gate
for a fifth of the docs. So `SELF_TEST_*` below pins each form against literal
fixtures, asserted BEFORE the real scan, including forms that must NOT match.
The floors stay as a backstop against total breakage; the fixtures are what pin
the individual forms, because a count over the tree cannot tell a document that
stopped using a spelling from a matcher that stopped reading it.

One limit the fixtures make visible rather than hide: the row matcher reads a
path only inside backticks, so an un-backticked path in the catalogue is a row
this gate cannot see. It is pinned as a must-not-match fixture, so widening it
is a deliberate act with a failing fixture to update instead of a silent change
in what gets checked.

A second limit was closed rather than documented. Direction 3 read `@router.`
only, so the seven `@dual_router` routes in that file were outside it and
"every route this file defines is catalogued" was checking 22 of 29. All seven
were catalogued, so the verdict never moved -- which is exactly why nobody
would have found it from a failing run.

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
# BOTH routers, because this file uses both. `@dual_router` mounts a route on
# the native surface as well, and the seven it carries here -- the state-version
# reads and the three lock actions -- were outside this gate's third direction
# entirely: "every route tfe_v2.py defines must be catalogued" was checking 22
# of 29. All seven are catalogued, so widening this changed no verdict; what it
# changes is that the gate now means what its own docstring says.
DECORATOR = re.compile(r'@(?:dual_)?router\.(get|post|put|patch|delete)\(\s*"([^"]+)"')
#: A path parameter's NAME differs between the two sources by design: the
#: catalogue writes `{id}` for a reader, the contract writes `{workspace_id}`
#: from the handler's own signature. Compare shape, not spelling.
PLACEHOLDER = re.compile(r"\{[^}]*\}")

TFE_PREFIX = "/api/tfe/v2"

#: `(method, path)` -> the key it must normalise to. The query-string pair is the
#: form that reported a served route as missing on this gate's first run, and the
#: two placeholder spellings are here as a PAIR: the property is that they
#: produce the same key, because the catalogue writes `{id}` for a reader and the
#: contract writes the handler's own parameter name.
SELF_TEST_NORMALISE: list[tuple[tuple[str, str], str]] = [
    (("GET", "/api/tfe/v2/ping"), "GET /api/tfe/v2/ping"),
    (("get", "/api/tfe/v2/ping"), "GET /api/tfe/v2/ping"),
    (("GET", "/api/tfe/v2/workspaces/{id}/runs"), "GET /api/tfe/v2/workspaces/{}/runs"),
    (
        ("GET", "/api/tfe/v2/workspaces/{workspace_id}/runs"),
        "GET /api/tfe/v2/workspaces/{}/runs",
    ),
    (("GET", "/api/tfe/v2/runs/{id}?include=task_stages"), "GET /api/tfe/v2/runs/{}"),
    (
        ("PUT", "/api/tfe/v2/state-versions/{id}/content "),
        "PUT /api/tfe/v2/state-versions/{}/content",
    ),
]

#: Markdown fragment -> `(expected_served, declared_unserved)`. Each entry is a
#: form the real catalogue uses, or a form that must not be read as a route row.
SELF_TEST_ROWS: list[tuple[str, list[str], list[str]]] = [
    (
        "| GET | `/api/tfe/v2/ping` | client init | configures every connection |",
        ["GET /api/tfe/v2/ping"],
        [],
    ),
    # Two columns only -- the Discovery table's real shape. A matcher that
    # required the go-tfe and caller cells would drop it.
    ("| GET | `/.well-known/terraform.json` |", ["GET /.well-known/terraform.json"], []),
    # The marker lives anywhere after the path cell, and is matched case-folded.
    (
        "| DELETE | `/api/tfe/v2/organizations/default/workspaces/{name}` | "
        "`Workspaces.Delete` | `remote/backend_state.go:145` — **NOT SERVED** |",
        [],
        ["DELETE /api/tfe/v2/organizations/default/workspaces/{}"],
    ),
    (
        "| POST | `/api/tfe/v2/runs` | `Runs.Create` | not served yet |",
        [],
        ["POST /api/tfe/v2/runs"],
    ),
    # A query string shows what the CLI asks for and is not part of identity.
    (
        "| GET | `/api/tfe/v2/runs/{id}?include=task_stages` | `Runs.ReadWithOptions` | x |",
        ["GET /api/tfe/v2/runs/{}"],
        [],
    ),
    # Must NOT match: the header and separator rows, a body-catalogue row whose
    # first cell is an attribute rather than a verb, and a path named in prose.
    ("| Method | Path | go-tfe method | Caller |\n|---|---|---|---|", [], []),
    ("| `name` | `name-field` | The workspace name |", [], []),
    ("A reader can call GET `/api/tfe/v2/ping` by hand.", [], []),
    # A DOCUMENTED LIMIT, not a desired refusal: a path outside backticks is a
    # row this gate cannot see. Pinned so that teaching it the bare form is a
    # deliberate change with a fixture to update.
    ("| GET | /api/tfe/v2/ping | client init | no backticks |", [], []),
]

#: Router source -> the own-route keys it must yield.
SELF_TEST_DECORATORS: list[tuple[str, list[str]]] = [
    ('@router.get("/ping")', ["GET /api/tfe/v2/ping"]),
    (
        '@router.patch("/workspaces/{workspace_id}")',
        ["PATCH /api/tfe/v2/workspaces/{}"],
    ),
    (
        '@router.post("/workspaces/{workspace_id}/relationships/tags", status_code=204)',
        ["POST /api/tfe/v2/workspaces/{}/relationships/tags"],
    ),
    # The multi-line form, which the real file does not use today but which a
    # route gaining dependencies would arrive as.
    (
        '@router.delete(\n    "/state-versions/{id}",\n    status_code=204,\n)',
        ["DELETE /api/tfe/v2/state-versions/{}"],
    ),
    # Must NOT match: a verb this gate does not read.
    ('@router.api_route("/ping", methods=["GET"])', []),
    # A COMMENTED decorator IS read -- the matcher is unanchored. Measured, and
    # pinned rather than fixed: the only way this can be wrong is by reporting a
    # route as uncatalogued, which fails loudly and is read by a human, so the
    # cost of the over-reach is a message rather than a missed promise.
    # Must NOT be read: a commented-out decorator names a route nobody serves,
    # and reporting it as uncatalogued sends a reader after something that does
    # not exist. The matcher is unanchored, so this is the skip that stops it.
    ('# @router.get("/retired")', []),
    ('    # @dual_router.get("/retired-too")', []),
    # MUST be read: `@dual_router` mounts on this surface as well as the native
    # one, and the seven such routes here were outside direction 3 entirely.
    (
        '@dual_router.post("/workspaces/{workspace_id}/actions/lock")',
        ["POST /api/tfe/v2/workspaces/{}/actions/lock"],
    ),
]


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


def catalogue_from(text: str) -> tuple[set[str], set[str]]:
    """`(expected_served, declared_unserved)` from a catalogue's own rows.

    Pure, over text, so `SELF_TEST_ROWS` drives exactly what the scan drives.
    """
    expected: set[str] = set()
    unserved: set[str] = set()
    for method, path, rest in ROW.findall(text):
        key = _normalise(method, path)
        (unserved if "NOT SERVED" in rest.upper() else expected).add(key)
    return expected, unserved


def _catalogue() -> tuple[set[str], set[str]]:
    return catalogue_from(DOC.read_text())


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


def prose_routes_from(doc: str) -> set[str]:
    """Routes the catalogue names in PROSE rather than as a table row.

    A table row is a promise to a CLI; prose is how this document records a
    route that is SERVED and that no CLI calls. One such route exists -- the
    state-version list, "also served, on both surfaces, though no CLI calls it"
    -- and a row for it would have to leave the Caller column empty, asserting
    a consumption that does not exist.

    So direction 3 reports such a route as a NOTE rather than a failure, and the
    exemption is DERIVED from the document: it holds only while the document
    actually names the method and the path. Deleting the sentence re-arms the
    failure, which is the property a hardcoded allowlist here would not have.
    """
    rows = {line for line in doc.split("\n") if line.lstrip().startswith("|")}
    prose = "\n".join(line for line in doc.split("\n") if line not in rows)
    found = set()
    # Both spellings, because the document uses both and a matcher reading only
    # one would report a documented route as undocumented. The method may sit
    # outside the backticks or inside them, and the state-version list -- the
    # one route this exemption exists for -- is written the second way.
    for pattern in (
        r"\b(GET|POST|PUT|PATCH|DELETE)\s+`([^`]+)`",
        r"`(GET|POST|PUT|PATCH|DELETE)\s+([^`]+)`",
    ):
        for method, path in re.findall(pattern, prose):
            found.add(_normalise(method, path.strip()))
    return found


def own_routes_from(text: str) -> set[str]:
    """Every route the CLI-contract router defines, as comparable keys.

    Commented-out decorators are skipped. The matcher is otherwise unanchored,
    so a `# @router.get("/x")` read as live would report a route that does not
    exist as uncatalogued -- loud rather than silent, which is the safe
    direction, but it would still send a reader after a route nobody serves.
    """
    live = "\n".join(line for line in text.split("\n") if not line.lstrip().startswith("#"))
    return {_normalise(method, TFE_PREFIX + path) for method, path in DECORATOR.findall(live)}


def _own_routes() -> set[str]:
    return own_routes_from(ROUTER.read_text())


def self_test() -> list[str]:
    """Pin every form the three matchers must read, before reading the tree.

    This is the gate on the gate. A narrowed pattern fails here naming the
    spelling it stopped reading, rather than quietly checking less of a
    catalogue that is still large enough to clear every floor.
    """
    out: list[str] = []
    for (method, path), want in SELF_TEST_NORMALISE:
        got = _normalise(method, path)
        if got != want:
            out.append(f"_normalise({method!r}, {path!r}) gave {got!r}, not {want!r}")
    for text, want_served, want_unserved in SELF_TEST_ROWS:
        served, unserved = catalogue_from(text)
        if sorted(served) != sorted(want_served) or sorted(unserved) != sorted(want_unserved):
            out.append(
                f"the row matcher no longer reads {text!r} the way it must: expected "
                f"served={sorted(want_served)} unserved={sorted(want_unserved)}, read "
                f"served={sorted(served)} unserved={sorted(unserved)}. Widen the pattern "
                "rather than the fixture -- a row spelling this gate cannot see is a "
                "promise it does not check."
            )
    for text, want_own in SELF_TEST_DECORATORS:
        got_own = own_routes_from(text)
        if sorted(got_own) != sorted(want_own):
            out.append(
                f"the decorator matcher no longer reads {text!r} the way it must: "
                f"expected {sorted(want_own)}, read {sorted(got_own)}."
            )
    return out


def main() -> int:
    # The gate's own health first: a broken matcher makes every comparison below
    # meaningless, so say so rather than reporting on what it can still see.
    broken = self_test()
    if broken:
        print("the CLI-surface-served gate cannot do its job:")
        for problem in broken:
            print(f"  - {problem}")
        return 1

    problems: list[str] = []
    notes: list[str] = []
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

    in_prose = prose_routes_from(DOC.read_text())
    for key in sorted(own - expected - unserved):
        if key in in_prose:
            notes.append(f"served and documented in prose, not promised as a row: {key}")
            continue
        problems.append(
            f"SERVED IN {ROUTER.name} BUT NOT CATALOGUED: {key}\n"
            "      That file is the CLI-contract router, so a route in it is surface we "
            "promise. Add the row, or move the route to a native router. If it is served "
            "and no CLI calls it, say so in prose and this becomes a note."
        )

    if problems:
        print(f"{DOC.name} and the server disagree:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    for note in notes:
        print(f"  note: {note}")
    print(
        f"OK — {len(expected)} catalogued routes all served, "
        f"{len(unserved)} marked NOT SERVED and none of them is, "
        f"{len(own) - len(notes)} of {len(own)} routes in {ROUTER.name} catalogued as "
        f"rows and {len(notes)} documented in prose. "
        f"{len(SELF_TEST_NORMALISE) + len(SELF_TEST_ROWS) + len(SELF_TEST_DECORATORS)} "
        "matcher fixtures hold."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
