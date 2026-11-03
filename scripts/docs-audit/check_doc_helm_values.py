#!/usr/bin/env python3
"""Every Helm-values snippet in the docs must survive `helm lint`.

WHY THIS EXISTS. `docs/pulumi.md` opened with a copy-pasteable block telling an
operator to set `api.config.engines.pulumi.enabled: true`. That switch was
withdrawn before 2.0 (#1986) and `values.schema.json` sets
`additionalProperties: false`, so the snippet did not quietly no-op --- it FAILED
`helm lint` and `helm upgrade`:

    at '/api/config': additional properties 'engines' not allowed

Following the page broke the install. Worse, `docs/upgrading-to-2.0.md` told
operators to REMOVE that exact block, so two pages gave opposite instructions and
the copy-pasteable one was wrong. A cold read of the repository found it; nothing
in CI could, because every existing gate reads prose, counts or links. None of
them had an opinion about whether a fenced YAML block is a thing an operator can
actually apply.

HELM IS THE AUTHORITY, NOT A REIMPLEMENTATION. The schema carries 122
`additionalProperties: false` clauses across ten `$defs`, and a hand-rolled key
walker would be a second implementation of the thing that rejects --- wrong in
ways nobody could see. So this writes each snippet to a file and runs the real
`helm lint`, which is exactly how the defect above was proven.

WHICH BLOCKS COUNT, derived rather than listed: a fenced `yaml` block is a Helm
values snippet when its top-level keys are all declared at the root of
`values.schema.json`. That is read from the schema itself, so a new top-level
section is covered the day it is added. A block mixing a known root key with an
unknown one is still checked, and failing is correct: that is the defect shape.
A block with NO known root key is some other kind of YAML (a Kubernetes object,
an `ansible.cfg` fragment, an inventory) and is skipped.

STDLIB ONLY --- no PyYAML, because classification needs the top-level keys and
nothing more, which a line matcher gets right for the block shapes docs use.
Needs `helm` on PATH, which is why this runs in the chart job rather than beside
the prose gates.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "helm" / "terrapod"
SCHEMA = CHART / "values.schema.json"

#: A floor, so a matcher that stops matching fails here instead of passing on
#: nothing. There are comfortably more than this in the tree today.
MIN_BLOCKS = 5

#: An UNDECLARED key is always a defect: the key does not exist, so the snippet
#: cannot be applied by anybody. A MISSING REQUIRED key is different -- a doc
#: legitimately shows a fragment illustrating one setting -- so that is a note
#: rather than a failure. Both come from helm's own message, so neither is a
#: judgement of ours.
UNDECLARED = re.compile(r"additional propert(?:y|ies).*not allowed")
MISSING = re.compile(r"missing propert(?:y|ies)")

#: A block an upgrade guide prints in order to say "delete this" is EXPECTED not
#: to lint, and the expectation is inverted: if it passes, the key still exists
#: and the upgrade note is wrong. Derived from a marker the docs already use on
#: the block's own first line, so there is no ledger to drift.
REMOVAL = re.compile(r"#.*\b(remove|delete|drop)\b", re.I)

FENCE = re.compile(r"^```ya?ml\s*$")
FENCE_END = re.compile(r"^```\s*$")
#: A top-level mapping key: no leading whitespace, no list dash.
ROOT_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):")


def schema_roots() -> set[str]:
    try:
        s = json.loads(SCHEMA.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"FAIL: cannot read {SCHEMA}: {exc}")
    roots = set((s.get("properties") or {}).keys())
    if not roots:
        sys.exit(
            "FAIL: values.schema.json declares no root properties, so this "
            "check cannot tell a Helm snippet from any other YAML. Fix the "
            "schema read rather than lowering the floor."
        )
    return roots


def blocks(path: pathlib.Path) -> list[tuple[int, str]]:
    out, lines, i = [], path.read_text().split("\n"), 0
    while i < len(lines):
        if FENCE.match(lines[i]):
            start = i + 1
            j = start
            while j < len(lines) and not FENCE_END.match(lines[j]):
                j += 1
            out.append((start + 1, "\n".join(lines[start:j])))
            i = j + 1
        else:
            i += 1
    return out


def main() -> int:
    if not CHART.is_dir():
        sys.exit(f"FAIL: no chart at {CHART}")
    roots = schema_roots()

    docs = sorted(
        [*(ROOT / "docs").rglob("*.md"), ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "llms.txt"]
    )
    problems: list[str] = []
    notes: list[str] = []
    checked = 0

    with tempfile.TemporaryDirectory() as td:
        for doc in docs:
            if not doc.is_file():
                continue
            for lineno, body in blocks(doc):
                keys = {m.group(1) for m in (ROOT_KEY.match(ln) for ln in body.split("\n")) if m}
                if not keys & roots:
                    continue  # not a Helm values snippet
                checked += 1
                f = pathlib.Path(td) / "snippet.yaml"
                f.write_text(body + "\n")
                p = subprocess.run(
                    ["helm", "lint", str(CHART), "-f", str(f)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                out = p.stdout + p.stderr
                first = next((ln for ln in body.split("\n") if ln.strip()), "")
                marked = bool(REMOVAL.search(first))
                undeclared = UNDECLARED.search(out)
                rel = doc.relative_to(ROOT)
                detail = "\n".join(
                    ln.strip()
                    for ln in out.split("\n")
                    if UNDECLARED.search(ln) or MISSING.search(ln)
                )

                if marked:
                    # Inverted: a block printed to be deleted must be refused.
                    if not undeclared:
                        problems.append(
                            f"{rel}:{lineno} — this block is marked for removal and "
                            f"the schema ACCEPTS it, so the key still exists and the "
                            f"upgrade note is wrong:\n        {first.strip()}"
                        )
                    continue

                if undeclared:
                    problems.append(
                        f"{rel}:{lineno} — an operator cannot apply this snippet:\n"
                        + "\n".join(f"        {d}" for d in detail.split("\n") if d)
                        + f"\n        keys: {sorted(keys & roots)}"
                    )
                elif p.returncode != 0 and MISSING.search(out):
                    notes.append(
                        f"{rel}:{lineno} — a fragment: lints only as part of a "
                        f"fuller block ({detail.splitlines()[0] if detail else 'missing required keys'})"
                    )

    if checked < MIN_BLOCKS:
        problems.append(
            f"only {checked} Helm-values snippet(s) were found, below the floor "
            f"of {MIN_BLOCKS}. The fence or root-key matcher has stopped "
            f"matching; fix it rather than lowering the floor."
        )

    if problems:
        print(f"check_doc_helm_values: {len(problems)} problem(s) over {checked} snippet(s)\n")
        for pr in problems:
            print(f"  {pr}\n")
        print(
            "  A values snippet in the docs is something an operator PASTES. "
            "A key the\n  schema refuses does not no-op -- it fails `helm lint` "
            "and `helm upgrade`, so\n  the page breaks the install it is trying "
            "to describe."
        )
        return 1

    for n in notes:
        print(f"  note: {n}")
    print(
        f"check_doc_helm_values: OK -- {checked} Helm-values snippet(s) across "
        f"the docs; every one an operator would paste whole applies, and "
        f"{len(notes)} {'is a fragment' if len(notes) == 1 else 'are fragments'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
