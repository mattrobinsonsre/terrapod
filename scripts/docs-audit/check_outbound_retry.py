#!/usr/bin/env python3
"""Every outbound HTTP call is routed through the retry helper, or says why not.

**This is the first application of a rule that `AGENTS.md` now states
explicitly: a defect class that recurs after being written down earns a gate,
because the words have been tested and failed.**

Bounded retry has been a hard requirement in the contributor guide for a long
time. It was broken seven times in one six-week window: by a whole package of
pull-through proxies at once, by the single runner-to-API call out of dozens that
lacked it (one refused connection killed an entire configure run, where every
sibling call survived the identical failure), and by nine downloads in one review
round. In each case the surrounding code already complied -- which is the tell,
and the reason a stated rule was never going to be enough. **Compliance by
neighbour is not compliance**, because a new call site copies the shape of the
work, not the shape of the policy.

The guide already allows the exemption this gate looks for: *"Deliberately
best-effort calls (e.g. telemetry) may skip retry but MUST say so in a comment."*
So this enforces the rule exactly as written -- route it, or say why not.

**Ratcheted, not retrospective.** There are more unrouted call sites today than
can be fixed in one pass, so the existing population is recorded in
`outbound_retry_baseline.json` and grandfathered. What the gate blocks is
GROWTH. Clearing the baseline down is R6's backlog sweep, one batch at a time.

**The matcher is whole-identifier, and that is load-bearing.** A substring test
on client-ish names matched `var_config.get("type")`, `summary_counts.get(...)`
and `vcs_conn_data.get("id")` -- because `_config`, `_counts` and `_conn` all
contain `_c` -- and reported 106 sites where there are 47, most of them ordinary
dict reads. A gate whose findings are mostly false positives is one people learn
to regenerate without reading, so the dict-read case is pinned as a fixture.

Regenerate the baseline with `UPDATE_OUTBOUND_RETRY=1`, reviewed like any
snapshot: the delta must be an addition you meant, and each addition must either
route through the helper or carry its reason.

Run from the repository root.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASELINE = pathlib.Path(__file__).with_name("outbound_retry_baseline.json")
SOURCE = ROOT / "services" / "terrapod"

_VERBS = {"get", "post", "put", "patch", "delete", "head", "request", "stream", "send"}

#: Modules whose "client" is not an HTTP client at all. Named rather than
#: pattern-matched, because the list is short and a pattern would be guessing.
_NOT_HTTP = ("redis/client.py", "redis/cluster.py")

#: The helper, and the module that defines it (which cannot route through itself).
_RETRY = re.compile(r"\b(?:a)?request_with_retry\b|_fetch_to_file_with_retry")
_HELPER_MODULE = "http_retry.py"

#: What counts as the comment the guide asks for.
_EXEMPT = re.compile(
    r"best.effort|no retry|not retried|deliberately unretried|retry would|"
    r"single.shot deliberately|telemetry",
    re.I,
)

# A floor rather than an equality, and deliberately: this population is MEANT
# to shrink as the backlog is cleared, so an equality would fail on every
# improvement. The other direction -- the population GROWING -- is caught by
# the baseline diff above, which is stronger than any count.
_MIN_SITES = 25

#: Fixtures, asserted before the scan. The dict-read entries are the ones that
#: matter: they are what the loose matcher got wrong.
_MUST = ("client", "http_client", "session", "_client", "httpx", "requests")
_MUST_NOT = ("var_config", "summary_counts", "vcs_conn_data", "cfg", "payload", "attrs")


def _clientish(name: str) -> bool:
    low = name.lower()
    return (
        low in ("httpx", "requests", "client", "session", "http", "aiohttp")
        or low.endswith("_client")
        or low.endswith("client")
        or low.endswith("_session")
        or low.endswith("session")
    )


#: The exemption the guide allows, as literals. Found by mutation: deleting the
#: exemption clause entirely changed nothing, because no call site in the tree
#: uses it today -- so the branch was unreachable and its behaviour untested,
#: while `AGENTS.md` promises it works. A gate must support what the guide
#: permits, and an unexercised branch is a promise with nothing behind it.
_EXEMPT_MUST = (
    "# best-effort: telemetry, so a failure must not fail the run",
    "# deliberately not retried -- the caller re-reads on the next cycle",
    "# no retry here: a slow upstream must not slow a servable read",
)
_EXEMPT_MUST_NOT = (
    "# fetch the manifest and parse it",
    "# the client is built per request",
)


def _self_test() -> list[str]:
    bad = []
    for n in _MUST:
        if not _clientish(n):
            bad.append(f"the client matcher no longer recognises {n!r}")
    for n in _MUST_NOT:
        if _clientish(n):
            bad.append(
                f"the client matcher now accepts {n!r} -- it has gone back to a "
                "substring test, which reports dict reads as HTTP calls"
            )
    for c in _EXEMPT_MUST:
        if not _EXEMPT.search(c):
            bad.append(f"the exemption clause no longer honours: {c}")
    for c in _EXEMPT_MUST_NOT:
        if _EXEMPT.search(c):
            bad.append(f"the exemption clause now accepts an ordinary comment: {c}")
    return bad


def collect() -> dict[str, list[str]]:
    """{relative path: [sorted "name.verb" keys]} for unrouted outbound calls."""
    found: dict[str, list[str]] = {}
    for f in sorted(SOURCE.rglob("*.py")):
        rel = str(f.relative_to(ROOT))
        if any(rel.endswith(x) for x in _NOT_HTTP) or rel.endswith(_HELPER_MODULE):
            continue
        try:
            text = f.read_text()
            tree = ast.parse(text)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        lines = text.split("\n")
        keys: set[str] = set()
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
                continue
            if n.func.attr not in _VERBS:
                continue
            b = n.func.value
            name = (
                b.id
                if isinstance(b, ast.Name)
                else (b.attr if isinstance(b, ast.Attribute) else "")
            )
            if not _clientish(name):
                continue
            # Routed, if the enclosing module uses the helper at all. Coarse on
            # purpose: a file that routes some calls and not others is a real
            # finding, but it is a READING finding -- this gate's job is the
            # file that routes nothing.
            if _RETRY.search(text):
                continue
            # Exempt, if a comment within three lines above says why.
            window = " ".join(lines[max(0, n.lineno - 4) : n.lineno])
            if _EXEMPT.search(window):
                continue
            keys.add(f"{name}.{n.func.attr}")
        if keys:
            found[rel] = sorted(keys)
    return found


def main() -> int:
    broken = _self_test()
    if broken:
        print("FAIL — this gate's own matcher is wrong before it scanned anything:\n")
        for b in broken:
            print(f"  {b}")
        return 1

    found = collect()
    total = sum(len(v) for v in found.values())

    if "--report" in sys.argv:
        print(f"{total} unrouted outbound call site(s) across {len(found)} file(s):\n")
        for rel in sorted(found):
            print(f"  {rel}")
            for k in found[rel]:
                print(f"      {k}")
        return 0

    if os.environ.get("UPDATE_OUTBOUND_RETRY"):
        BASELINE.write_text(json.dumps(found, indent=2, sort_keys=True) + "\n")
        print(f"baseline regenerated: {total} site(s) across {len(found)} file(s)")
        return 0

    if not BASELINE.exists():
        print("FAIL — no baseline. Create one with UPDATE_OUTBOUND_RETRY=1 and review it.")
        return 1

    if total == 0:
        print(
            "FAIL — found no outbound calls at all. Either every one is now routed "
            "(check with --report and celebrate) or this gate's matcher has stopped "
            "matching, which is the defect class it exists to catch."
        )
        return 1

    if total < _MIN_SITES and not os.environ.get("UPDATE_OUTBOUND_RETRY"):
        # Not a failure: the population SHOULD shrink. But a sudden collapse is
        # far more likely to be a broken matcher than a cleanup nobody mentioned.
        print(
            f"NOTE — only {total} site(s) found, below the recorded floor of "
            f"{_MIN_SITES}. If the backlog really has been cleared, regenerate the "
            "baseline and lower the floor in the same commit."
        )

    base = json.loads(BASELINE.read_text())
    new = [(rel, k) for rel, keys in found.items() for k in keys if k not in set(base.get(rel, []))]

    if new:
        print(f"FAIL — {len(new)} outbound call site(s) with no retry and no reason:\n")
        for rel, k in sorted(new):
            print(f"  {rel}")
            print(f"      {k}")
        print(
            "\nBounded retry is a hard requirement in AGENTS.md, and it has been broken\n"
            "seven times in code whose neighbours already complied. So either:\n"
            "  - route the call through `arequest_with_retry` / `request_with_retry`; or\n"
            "  - say in a comment just above it why it is deliberately best-effort.\n"
            "Then regenerate with UPDATE_OUTBOUND_RETRY=1."
        )
        return 1

    cleared = sum(1 for rel, keys in base.items() for k in keys if k not in set(found.get(rel, [])))
    extra = f", {cleared} baseline site(s) now routed or removed" if cleared else ""
    print(f"PASS — {total} unrouted site(s) inventoried, none new{extra}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
