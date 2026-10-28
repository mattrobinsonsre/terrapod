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

#: Directories that hold go.mod files which are not ours. `.claude/worktrees/`
#: carries a full checkout per past agent run -- a root-level walk returns
#: hundreds of lines of which nine matter -- and `.conformance/` vendors a
#: third-party suite pinned to Go 1.15-1.20, so including it would report a
#: permanent, unfixable divergence.
EXCLUDED = (".claude", ".conformance", "node_modules", ".git")

#: A floor, so a walk that stopped matching fails instead of passing on nothing.
#: Nine modules today; this is deliberately below that rather than equal to it,
#: because adding a module is routine and should not fail this gate.
MIN_MODULES = 7

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
    print(f"OK: all {len(mods)} Go modules declare `go {version}`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
