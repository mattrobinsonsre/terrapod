#!/usr/bin/env python3
"""The docs that quote the promoted-log notice quote the real one (#2526).

When a run ends with no stored log, the reconciler promotes the live Redis
snapshot to storage so the run is not left with nothing once the 300-second key
expires. That snapshot is a bounded TAIL, so the promoted object opens with a
line saying it is one -- `LOG_TAIL_NOTICE` in
`services/terrapod/services/run_reconciler.py`.

An operator who meets a short log reads that line and searches the docs for it.
So `docs/ansible-configure.md`'s troubleshooting row quotes its opening words,
and a doc that quotes a message goes stale SILENTLY: change the constant and the
row still reads perfectly while matching nothing anybody will ever see. That is
worse than no example, because the reader concludes the symptom is undocumented.

**A docs-audit gate and not a pytest one**, which is the whole reason this file
exists rather than an assertion beside the notice's other tests:
`docker/Dockerfile.test` copies exactly one file out of `docs/`
(`docs/deprecations.md`), so a test reading any other would fail in CI with a
`FileNotFoundError` naming a path that is right in the checkout. This job runs
against a full checkout.

**Read as TEXT, never imported.** Importing `terrapod.services.run_reconciler`
drags in the models and so SQLAlchemy and the rest of the API's dependency
closure, which the job running this installs none of -- the recorded way a
docs-audit gate passes locally and fails in CI with `ModuleNotFoundError`. The
constant is an implicitly-concatenated bytes literal, which Python's own parser
joins into ONE `ast.Constant`, so `ast` gives the exact value with no
reassembly of ours.

**It refuses rather than guesses.** A constant it cannot read -- built by a
call, an f-string, a name -- is a construction this gate cannot vouch for, so it
fails and says so instead of silently checking nothing.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SOURCE = Path("services/terrapod/services/run_reconciler.py")
CONSTANT = "LOG_TAIL_NOTICE"

#: Each doc that quotes the notice, and how much of it that doc must carry.
#:
#: A PREFIX rather than the whole line, because the notice is a paragraph and a
#: table row quotes its opening words -- which is what a reader greps. Long
#: enough that it cannot match by accident, short enough that rewording the rest
#: of the notice does not fail a row that is still correct.
QUOTED_PREFIX = "The runner did not store this phase's log"
DOCS = (Path("docs/ansible-configure.md"),)


def _constant_value(source: Path, name: str) -> str:
    """The value of a module-level string/bytes constant, read from the source."""
    tree = ast.parse(source.read_text())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if name not in targets:
            continue
        if not isinstance(node.value, ast.Constant):
            raise SystemExit(
                f"FAIL — {source}:{node.lineno}: {name} is no longer a plain literal "
                f"({type(node.value).__name__}). Teach this reader the new shape -- do "
                "NOT reimplement the value here, which would make this gate check itself."
            )
        value = node.value.value
        if isinstance(value, bytes):
            return value.decode()
        if isinstance(value, str):
            return value
        raise SystemExit(f"FAIL — {source}: {name} is a {type(value).__name__}, not text")
    raise SystemExit(
        f"FAIL — {name} is not assigned at module level in {source}. If it moved, move "
        "this gate with it; if it is gone, the docs quoting it are now quoting nothing."
    )


def main() -> int:
    notice = _constant_value(SOURCE, CONSTANT)

    problems: list[str] = []
    if QUOTED_PREFIX not in notice:
        problems.append(
            f"{SOURCE}: {CONSTANT} no longer contains {QUOTED_PREFIX!r}, so every doc "
            "quoting it quotes text no operator will ever see. Update the constant and "
            "the rows together, or update this gate's prefix."
        )
    for doc in DOCS:
        if not doc.exists():
            problems.append(f"{doc}: does not exist")
        elif QUOTED_PREFIX not in doc.read_text():
            problems.append(
                f"{doc}: does not quote the promoted-log notice. An operator meeting a "
                f"short log searches this page for {QUOTED_PREFIX!r} and finds nothing."
            )

    if problems:
        print(f"FAIL — {len(problems)} problem(s) with the promoted-log notice:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(
        f"PASS — {CONSTANT} opens {QUOTED_PREFIX!r} and all "
        f"{len(DOCS)} doc(s) that quote it carry the same words."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
