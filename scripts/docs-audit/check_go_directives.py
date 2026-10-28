#!/usr/bin/env python3
"""Every Go module declares the same `go` directive.

**This gate was asserted by a comment and did not exist.** `ci.yml`'s
`go-modules` job reads its toolchain from ONE module -- `go-terrapod/go.mod` --
and its own comment explains why that is safe: *"Any module's directive will do
-- they are kept in step by `check_go_directives.py`"*. That script was never
written. Nine modules happened to agree, so the claim was true by luck rather
than by mechanism, which is the shape this repository keeps recording: a
sentence that reads as a guarantee with nothing behind it.

**What it costs when they diverge.** Official Go images pin `GOTOOLCHAIN=local`
and `setup-go` honours the file it is given, so a module needing a NEWER
directive than `go-terrapod` is built by a toolchain that refuses it. The error
names the toolchain rather than the mismatch, and it has already cost this
project a session once -- a stale `golang:1.26` tag against a `go 1.26.9`
directive made Tilt build nothing for forty minutes while the API still answered
200.

**Derived, never listed.** A hand-kept list is how `loadtest` came to be the one
module with no lint or test job, and how `go-audit` came to miss it for months.
The module set comes from the tree.

Run from the repository root.
"""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

#: Directories that hold go.mod files which are not ours. `.claude/worktrees/`
#: carries a full checkout per past agent run -- a root-level walk returns
#: hundreds of lines of which nine matter -- and `.conformance/` vendors a
#: third-party suite pinned to Go 1.15-1.20, so including it would report a
#: permanent, unfixable divergence.
EXCLUDED = (".claude", ".conformance", "node_modules", ".git")

#: A floor, so a walk that stopped matching fails instead of passing on nothing.
#: Nine modules today; this is deliberately below that rather than equal to it,
#: because adding a module is routine and should not fail this gate.
#:
#: That slack is right HERE and wrong in two other places, which is the whole of
#: the third clause below: this gate asks "do the modules agree", so a tenth one
#: arriving is not a defect. `ci.yml`'s two floors answer "is every module
#: analysed", where slack is exactly the hole.
MIN_MODULES = 7

#: The `ci.yml` jobs whose module floor must EQUAL the live module count.
#:
#: Both derive their module list from the tree and then refuse to proceed if the
#: count looks wrong -- and in both, that floor is the only thing standing between
#: "the finder stopped matching" and a green run over a fraction of the Go code.
#: CodeQL's Go extractor sees only what the build compiles, so a module left out
#: of the loop is unanalysed with no signal at all; `host-checks`' own
#: `(go-audit)` step is the same shape, and its history is the argument -- it used
#: a hand-kept six-name list, which is how `loadtest` came to have never been
#: audited.
#:
#: These are JOB ids, not the step labels. The second floor lives in a step called
#: `(go-audit) Audit every Go module`, and reading that label as the job name is
#: what put the wrong id in #2275 -- the recorded "a CI job's display name is not
#: its YAML id" trap, arriving through a step.
#:
#: Both sat at 7 against nine modules (#2275), so either could have silently lost
#: two. A new module failing this clause is the POINT: it names the sites to
#: update rather than leaving them to be found by a scanner's silence.
COVERAGE_FLOOR_JOBS = ("codeql", "host-checks")

#: The floor comparison inside those jobs' shell. Scoped to the `mods` array
#: rather than any `-lt`, so an unrelated numeric test in the same block is not
#: mistaken for a module floor.
_FLOOR = re.compile(r"\$\{#mods\[@\]\}\"?\s*-lt\s+(\d+)")

_GO_DIRECTIVE = re.compile(r"^go\s+(\S+)\s*$", re.MULTILINE)


def go_mods(root: pathlib.Path) -> list[pathlib.Path]:
    """Every `go.mod` that belongs to this project."""
    out = []
    for path in root.rglob("go.mod"):
        rel = path.relative_to(root)
        if any(part in EXCLUDED for part in rel.parts):
            continue
        out.append(path)
    return sorted(out)


def directive(path: pathlib.Path) -> str | None:
    """The `go` directive, or None when the file does not declare one."""
    m = _GO_DIRECTIVE.search(path.read_text())
    return m.group(1) if m else None


def coverage_floors(root: pathlib.Path) -> tuple[dict[str, int], list[str]]:
    """Each coverage job's module floor, read out of `ci.yml`.

    Parsed as YAML rather than grepped, because a floor is only meaningful
    attributed to its JOB -- the same `-lt N` text elsewhere in the workflow is a
    different claim, and this clause is about exactly the two jobs whose floor is
    a coverage gate.

    A named job that is absent, or declares no floor, or declares more than one,
    is reported as a PROBLEM rather than skipped: a construction this cannot read
    is one it cannot vouch for, and silently passing over it is how the floors
    came to be stale in the first place.
    """
    workflow = root / ".github" / "workflows" / "ci.yml"
    if not workflow.is_file():
        return {}, [f"{workflow} is missing"]

    jobs = (yaml.safe_load(workflow.read_text()) or {}).get("jobs") or {}
    floors: dict[str, int] = {}
    problems: list[str] = []
    for job in COVERAGE_FLOOR_JOBS:
        spec = jobs.get(job)
        if spec is None:
            problems.append(f"ci.yml declares no job named {job!r}")
            continue
        # `str()` because YAML types a bare `run: true` as a boolean, and a gate
        # that dies on ordinary input reports CI as unwell rather than its property.
        shell = "\n".join(str(step.get("run", "")) for step in (spec.get("steps") or []))
        hits = _FLOOR.findall(shell)
        if len(hits) != 1:
            problems.append(f"job {job!r} declares {len(hits)} module floors, expected exactly 1")
            continue
        floors[job] = int(hits[0])
    return floors, problems


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[2]
    mods = go_mods(root)

    if len(mods) < MIN_MODULES:
        print(
            f"FAIL: found only {len(mods)} go.mod files (expected at least {MIN_MODULES}). "
            "The walk has probably stopped matching rather than the modules having been "
            "deleted -- check EXCLUDED and the rglob.",
            file=sys.stderr,
        )
        return 1

    found: dict[str, list[str]] = {}
    missing: list[str] = []
    for path in mods:
        name = str(path.parent.relative_to(root)) or "."
        d = directive(path)
        if d is None:
            missing.append(name)
            continue
        found.setdefault(d, []).append(name)

    if missing:
        print(
            "FAIL: module(s) declare no `go` directive, so nothing pins their toolchain: "
            + ", ".join(missing),
            file=sys.stderr,
        )
        return 1

    if len(found) > 1:
        print(
            "FAIL: the Go modules do not agree on a `go` directive, and `ci.yml`'s "
            "`go-modules` job reads its toolchain from ONE of them -- so the modules on "
            "the other version are built by a toolchain that may refuse them, with an "
            "error naming the toolchain rather than this mismatch.\n",
            file=sys.stderr,
        )
        for version, names in sorted(found.items()):
            print(f"  {version}: {', '.join(sorted(names))}", file=sys.stderr)
        print(
            "\nPick one version and set it in every module, then update "
            "`cache-dependency-path` if a module was added.",
            file=sys.stderr,
        )
        return 1

    version = next(iter(found))

    # Second clause: the prose that LISTS the modules lists all of them.
    #
    # `AGENTS.md` and `llms.txt` both name the modules rather than counting
    # them, because a number in prose has nothing enforcing it -- and both were
    # wrong when this was written, saying "four" and "five" against eight. Named
    # is better only if the naming is checked, so this is what makes it so. For
    # `llms.txt` especially: an agent reading an incomplete map cannot tell that
    # a module is missing, which is the inverse of that file's own no-hallucinated-
    # entries rule and just as misleading.
    listing = [pathlib.Path("AGENTS.md"), pathlib.Path("llms.txt")]
    names = sorted(str(p.parent.relative_to(root)) for p in mods)
    unnamed: list[str] = []
    for doc in listing:
        if not doc.is_file():
            print(f"FAIL: {doc} is missing", file=sys.stderr)
            return 1
        text = doc.read_text(errors="replace")
        for name in names:
            if f"`{name}/`" not in text:
                unnamed.append(f"{doc}: does not name `{name}/`")

    if unnamed:
        print(
            "FAIL: a document that lists the Go modules has left one out. Both files name "
            "them instead of counting them, so the list IS the claim -- and an agent reading "
            "an incomplete one cannot tell.\n",
            file=sys.stderr,
        )
        for u in unnamed:
            print(f"  {u}", file=sys.stderr)
        return 1

    # Third clause: the two coverage floors in `ci.yml` equal the module count.
    floors, problems = coverage_floors(root)
    if problems:
        print(
            "FAIL: a `ci.yml` module floor could not be read, so this clause cannot\n"
            "vouch for it. A renamed job or a rewritten loop must update\n"
            "COVERAGE_FLOOR_JOBS, not be passed over in silence.\n",
            file=sys.stderr,
        )
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    wrong = {job: n for job, n in floors.items() if n != len(mods)}
    if wrong:
        print(
            f"FAIL: {len(mods)} Go modules are in the tree, and a `ci.yml` coverage\n"
            "floor disagrees. That floor is the only thing that fails when the module\n"
            "finder stops matching, so a floor BELOW the real count lets the job\n"
            "analyse a fraction and report success.\n",
            file=sys.stderr,
        )
        for job, n in sorted(wrong.items()):
            direction = "below" if n < len(mods) else "above"
            print(
                f"  job {job}: floor is {n}, {direction} the {len(mods)} modules in the tree",
                file=sys.stderr,
            )
        print(
            f"\nSet each to {len(mods)}, or name the modules the job deliberately skips.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: all {len(mods)} Go modules declare `go {version}`, both AGENTS.md "
        f"and llms.txt name every one, and both ci.yml coverage floors are "
        f"{len(mods)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
