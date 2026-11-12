#!/usr/bin/env python3
"""No Go consumer addresses the deprecated `/api/terrapod/v1` native alias.

`/api/v1` is canonical; `/api/terrapod/v1` is an alias kept for the support
window, with a sunset recorded in `docs/deprecations.md`. The SDK and the
provider addressed the alias throughout -- 176 code lines between them -- and
`docs/deprecations.md` makes a promise on our behalf about it: an operator's
action is "take a newer version; the prefix is internal to them". So the move
was ours to make, and nothing tracked it (#2497).

Fixing the instance is not the fix. The next method written by copying the one
above it reintroduces a literal, and no existing gate can see it: the route and
attribute contracts describe what the SERVER serves, and `surface.golden`
records the exported Go surface rather than request paths. Hence this.

# Why the rule is about CODE lines rather than about files

A comment may legitimately name the alias -- `go-terrapod/version.go` explains
beside `MinimumAPIVersion` what the SDK used to address and why it moved, and
two fixtures in `variable_sets_test.go` record why they no longer fold a native
path onto the TFE spelling. Banning the string outright would delete the
history that stops somebody "restoring" the alias, so the rule is that no line
of code may REQUEST it while prose may describe it. That is also exactly the
split #2497 measured the sites with, so the gate and the issue agree on what
they are counting.

# What it cannot catch

A path assembled from pieces (`"/api/" + legacy + "/v1/..."`). Nothing in the
tree does that today and a literal is the shape a copied method takes, so the
cost of chasing it is not worth the false positives; stated here rather than
left to be discovered.

Run from the repository root: `python3 scripts/docs-audit/check_native_prefix.py`
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

ALIAS = "/api/terrapod/v1"

#: EVERY tracked Go file, rather than a list of the modules that have the defect
#: today. A list is a ledger: `mcp` and `migrate` hold no native path literal in
#: production and still broke on this change, because their own `httptest`
#: fixtures assert the SDK's paths -- so the modules worth scanning were not the
#: ones the work started from, and the next one will not be either. A module with
#: nothing to find costs nothing to scan.
GO_GLOBS = ("*.go",)
#: Not ours: agent worktrees and the vendored OCI conformance suite both carry
#: full checkouts, which `git ls-files` would otherwise count.
SKIP_DIRS = (".claude/", ".conformance/")

#: The ONE code site that may name the alias, with the reason, because a bare
#: path allow-list is how an exemption outlives its justification. This fake
#: strips whichever surface prefix a request arrives on so the test does not
#: depend on which one the provider addresses; dropping the alias from its list
#: would narrow a deliberately prefix-agnostic fixture for no gain.
EXEMPT_CODE = {
    "provider/internal/provider/oidc_audiences_cycle_test.go": (
        "a deliberately prefix-agnostic fake: it enumerates every surface "
        "prefix in order to strip it, rather than requesting one"
    ),
}

#: A matcher that stops matching must fail rather than pass on nothing.
MIN_FILES_SCANNED = 400
#: And the history this gate protects must still be there. Falling to zero means
#: either the prose was deleted or the comment detector broke; both are worth a
#: failure, because the comments are what stop the alias being "restored".
MIN_HISTORY_MENTIONS = 2

_COMMENT = re.compile(r"^\s*(//|/\*|\*)")


def _is_comment(line: str) -> bool:
    """True for a line whose content is prose.

    Deliberately crude: Go has no comment syntax that starts mid-line and still
    leaves code after it on the same line, so "the line begins with a comment
    marker" is exact for a whole-line comment, and a trailing `// ...` after
    code leaves the code visible to the scan -- which is the direction that
    matters.
    """
    return bool(_COMMENT.match(line))


def _tracked_go_files() -> list[str]:
    out = subprocess.run(
        [
            "git",
            "ls-files",
            "--",
            *GO_GLOBS,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(p for p in out.split("\n") if p.endswith(".go") and not p.startswith(SKIP_DIRS))


def _self_test() -> list[str]:
    """Literal fixtures for every spelling, asserted before the scan.

    A coverage floor cannot do this job: narrowing the comment detector shrinks
    what is checked while the floor still passes, and a doc that loses its last
    mention is benign where a detector that loses a form is not. So each form is
    pinned by a case that must classify a particular way, including cases that
    must NOT be treated as prose.
    """
    cases = [
        ("// the old `/api/terrapod/v1` alias", True),
        ("//\t/api/terrapod/v1/labels is the alias", True),
        ("   * /api/terrapod/v1/roles", True),
        ("  /* /api/terrapod/v1 */", True),
        ('\tdata, err := c.Get(ctx, "/api/terrapod/v1/roles")', False),
        ('\t\tif path != "/api/terrapod/v1/x" {', False),
        ('\tc.Get(ctx, "/api/v1/roles") // not /api/terrapod/v1 any more', False),
    ]
    bad = []
    for line, want_comment in cases:
        if _is_comment(line) is not want_comment:
            bad.append(
                f"self-test: {line!r} classified as {'prose' if not want_comment else 'code'}"
            )
    return bad


def main() -> int:
    problems = _self_test()
    if problems:
        for p in problems:
            print(f"FAIL — {p}")
        print(
            "The detector no longer agrees with its own fixtures; the scan below "
            "would be measuring something other than what this gate claims."
        )
        return 1

    files = _tracked_go_files()
    if len(files) < MIN_FILES_SCANNED:
        print(
            f"FAIL — scanned only {len(files)} tracked Go files, below the floor "
            f"of {MIN_FILES_SCANNED}; the matcher has stopped matching"
        )
        return 1

    offenders: list[str] = []
    history = 0
    for rel in files:
        text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        if ALIAS not in text:
            continue
        for n, line in enumerate(text.split("\n"), 1):
            if ALIAS not in line:
                continue
            if _is_comment(line):
                history += 1
                continue
            if rel in EXEMPT_CODE:
                continue
            offenders.append(f"{rel}:{n}: {line.strip()[:120]}")

    if offenders:
        print(
            f"FAIL — {len(offenders)} Go code line(s) still request the deprecated {ALIAS} alias:"
        )
        for o in offenders:
            print(f"    {o}")
        print(
            f"\n{ALIAS} is sunset in v2.1.0 (docs/deprecations.md). Address "
            "/api/v1 instead; the SDK's minimum server version "
            "(go-terrapod MinimumAPIVersion) is what makes that safe."
        )
        return 1

    if history < MIN_HISTORY_MENTIONS:
        print(
            f"FAIL — only {history} comment(s) mention {ALIAS}, below the floor of "
            f"{MIN_HISTORY_MENTIONS}. The prose recording why the SDK moved is what "
            "stops it being moved back; losing it is a regression, not a tidy-up."
        )
        return 1

    print(
        f"OK — {len(files)} tracked Go files: no code line requests {ALIAS} "
        f"({history} comment(s) record the history, "
        f"{len(EXEMPT_CODE)} exempt code site)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
