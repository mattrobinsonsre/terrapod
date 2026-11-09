#!/usr/bin/env python3
"""A provider attribute whose TYPE changed needs a state upgrader (#2452).

`allow_labels` and `deny_labels` went from map(string) to map(list(string)) on
BOTH `terrapod_role` and `terrapod_policy_set` in #1480, and neither declared a
schema `Version` or implemented `ResourceWithUpgradeState`. State written by a
1.x provider therefore could not be read AT ALL: every plan failed with
`Unable to Read Previously Saved State for UpgradeResourceState`, and it failed
even after the configuration had been updated to the new type -- the
configuration was never the problem.

It was reported for one resource. The other was found only by auditing all of
them, which is what this gate automates: a second instance shipping to GA would
have been worse than the first, and nothing in `go build`, `go vet` or the test
suite looks at a schema across a release boundary.

WHY A SCRIPT AND NOT A GO TEST. The comparison needs the previous release's
source, which means git -- and the provider's tests run against a checked-out
tree with no release history to consult. `scripts/docs-audit/` is the house
home for a gate that needs a full checkout (`check_token_mints.py` says so in
its own docstring).

WHAT IT CANNOT SEE, stated rather than implied: it compares the newest 1.x tag
with the working tree, so a type that changed and changed BACK reads as
unchanged, and a resource added since that tag is skipped -- correctly, because
no 1.x state of it can exist. It parses the schema literal rather than
evaluating it, so a type built by a helper call is reported as unreadable
rather than guessed at.

STDLIB ONLY.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

#: Attribute declarations inside a schema literal: `"name": schema.Kind{`.
ATTR = re.compile(r'"([a-z0-9_]+)":\s*schema\.(\w+)\{', re.M)
ELEM = re.compile(r"ElementType:\s*([^,\n]+)")
#: A resource implements the upgrade interface.
UPGRADES = re.compile(r"func\s*\(\s*\w+\s*\*\w+\s*\)\s*UpgradeState\s*\(")
#: A non-zero schema version. The value may be a literal or a constant, so the
#: constant is resolved in the same file -- `Version: schemaVersion` where that
#: constant is 0 reads as versioned to any matcher that stops at the
#: identifier, which is a way for this gate to pass on exactly the defect it
#: exists to catch. Found by mutation, not by reading.
VERSION = re.compile(r"Version:\s*(\w+)")
CONST = re.compile(r"^\s*const\s+(\w+)\s*=\s*(\d+)\s*$", re.M)


def schema_version(src: str) -> int | None:
    """The declared schema version, following one level of constant."""
    m = VERSION.search(src)
    if m is None:
        return None
    raw = m.group(1)
    if raw.isdigit():
        return int(raw)
    for name, value in CONST.findall(src):
        if name == raw:
            return int(value)
    return None


MIN_RESOURCES = 25


def git(*args: str) -> str | None:
    # check=False deliberately: a missing path at an old tag is the ANSWER
    # (the resource did not exist then), not an error.
    r = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return r.stdout if r.returncode == 0 else None


def newest_1x_tag() -> str | None:
    out = git("tag", "--list", "v1.*", "--sort=-v:refname") or ""
    for line in out.split("\n"):
        if re.fullmatch(r"v1\.\d+\.\d+", line.strip()):
            return line.strip()
    return None


def attributes(src: str) -> dict[str, tuple[str, str]]:
    """name -> (schema kind, element type as written, or '')."""
    out: dict[str, tuple[str, str]] = {}
    for m in ATTR.finditer(src):
        name, kind = m.group(1), m.group(2)
        tail = src[m.end() : m.end() + 400]
        stop = tail.find("schema.")
        if stop != -1:
            tail = tail[:stop]
        e = ELEM.search(tail)
        out[name] = (kind, e.group(1).strip() if e else "")
    return out


def main() -> int:
    tag = newest_1x_tag()
    if tag is None:
        print("check_provider_state_upgraders: no v1.x tag to compare against", file=sys.stderr)
        return 1

    files = sorted((git("ls-files", "provider/internal/resources/*/resource.go") or "").split())
    if len(files) < MIN_RESOURCES:
        print(
            f"check_provider_state_upgraders: found {len(files)} resources, expected at least "
            f"{MIN_RESOURCES} -- the matcher has stopped matching rather than the tree shrinking",
            file=sys.stderr,
        )
        return 1

    problems: list[str] = []
    checked = changed = 0
    for path in files:
        name = path.split("/")[-2]
        then = git("show", f"{tag}:{path}")
        if then is None:
            continue  # Added since; no 1.x state of it can exist.
        checked += 1
        now = Path(path).read_text()
        a, b = attributes(then), attributes(now)
        diffs = [(k, a[k], b[k]) for k in sorted(set(a) & set(b)) if a[k] != b[k]]
        if not diffs:
            continue
        changed += 1

        version = schema_version(now)
        versioned = version is not None and version > 0
        upgrades = bool(UPGRADES.search(now))
        if versioned and upgrades:
            continue

        detail = "; ".join(f"{k}: {x[0]}<{x[1]}> -> {y[0]}<{y[1]}>" for k, x, y in diffs)
        missing = []
        if not versioned:
            missing.append("a non-zero schema Version")
        if not upgrades:
            missing.append("an UpgradeState method")
        problems.append(
            f"  {name}: attribute type changed since {tag} and the resource has no "
            f"{' and no '.join(missing)}.\n      {detail}"
        )

    if problems:
        print(
            f"check_provider_state_upgraders: {len(problems)} resource(s) changed an attribute "
            f"type since {tag} without a state upgrader.\n",
            file=sys.stderr,
        )
        print("\n".join(problems), file=sys.stderr)
        print(
            "\n  A 1.x state cannot be read by the new schema, so EVERY plan fails with\n"
            "  `Unable to Read Previously Saved State for UpgradeResourceState` -- and it\n"
            "  fails even once the configuration has been updated. Set the schema Version\n"
            "  and implement UpgradeState; see provider/internal/stateupgrade.",
            file=sys.stderr,
        )
        return 1

    print(
        f"check_provider_state_upgraders: OK -- {checked} resource(s) compared against {tag}, "
        f"{changed} with a type change, all carrying an upgrader."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
