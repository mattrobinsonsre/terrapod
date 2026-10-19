#!/usr/bin/env python3
"""Derive which Ansible execution-environment images need building (#2154).

The matrix is DERIVED, never hand-kept: ask PyPI which ansible-core lines exist,
pick each line's newest DECLARED python from its own classifiers, and compare
that against what is already published. A new upstream line therefore needs no
code change -- the floor is pinned and the ceiling floats.

**Why the newest declared python rather than the fewest interpreters.** An EE for
an old core line is unpatchable by construction (2.17 dropped Python 2, so every
route to a 2.7 target is an EOL ansible-core), which makes the interpreter the one
dial we control -- so turn it the right way. Measured 2026-10-19: ten lines over
five bases, every pair installing and running a real playbook.

**Why the floor is 2.12.** It is the oldest line reaching a Python **2.6** target
(EL6); 2.13-2.16 reach **2.7** (EL7), and 2.16 is the newest line that does.
Anything older needs a py2 CONTROLLER, which does not build: on `python:2.7-slim`
both `ansible-core==2.11.12` and `ansible==2.9.27` die building PyYAML
(`AttributeError: cython_sources`), and pinning `Cython<3` only moves the failure
into a cffi build.

**Staleness is read from the image, not from a state file.** Each EE carries
`io.terrapod.ee.ansible-core` and `io.terrapod.ee.base-digest` labels, so "is this
current" is a comparison between what the registry holds and what PyPI and the
base tag say today -- no database, nothing to keep in step. Absent provenance
means rebuild, which makes the first run correct with nothing seeded.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

#: The oldest ansible-core line we publish an EE for. EL6 (python 2.6 targets).
#: Below this a py2 controller is required and cannot be built -- see the module
#: docstring. Pinned deliberately: only the ceiling floats.
FLOOR = (2, 12)

#: Stable releases only. ansible-core publishes release candidates; a workspace
#: may still *pin* one subject to `binary_cache.allow_prerelease`, and will get
#: the no-EE failure rather than a surprise image.
STABLE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

#: `Programming Language :: Python :: 3.12` -- the declared controller range.
CLASSIFIER = re.compile(r"^Programming Language :: Python :: (\d+\.\d+)$")

PYPI = "https://pypi.org/pypi/ansible-core"

#: The platforms the runner images are published for, so an EE matches wherever
#: an agent pool runs. Native runners per arch rather than QEMU.
ARCHES = ("amd64", "arm64")

#: A floor on the derivation itself. If PyPI answers but the shape has changed,
#: fail loudly rather than publish an empty or truncated matrix.
MIN_LINES = 8


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def latest_patch_per_line() -> dict[tuple[int, int], str]:
    """Every stable ansible-core line at or above the floor, at its newest patch."""
    index = _get_json(f"{PYPI}/json")
    latest: dict[tuple[int, int], tuple[int, str]] = {}
    for raw in index["releases"]:
        matched = STABLE.match(raw)
        if not matched:
            continue
        major, minor, patch = (int(part) for part in matched.groups())
        if (major, minor) < FLOOR:
            continue
        current = latest.get((major, minor))
        if current is None or patch > current[0]:
            latest[(major, minor)] = (patch, raw)
    return {line: version for line, (_, version) in latest.items()}


def declared_pythons(version: str) -> list[tuple[int, int]]:
    """The controller range a release declares, from its own classifiers.

    Classifiers rather than `requires_python`, which is a lower bound only and
    so says nothing about the top of the range -- the end that decides the base.
    """
    info = _get_json(f"{PYPI}/{version}/json")["info"]
    found = set()
    for classifier in info.get("classifiers") or []:
        matched = CLASSIFIER.match(classifier)
        if matched:
            major, minor = matched.group(1).split(".")
            found.add((int(major), int(minor)))
    return sorted(found)


def published_provenance(repository: str, tag: str) -> dict[str, str] | None:
    """The labels an already-published EE carries, or None if there is none.

    Reads the config blob from the registry without pulling the image. Any
    failure -- absent tag, unreadable config, no labels -- answers None, which
    the caller treats as "rebuild": absent provenance must never read as current.
    """
    probe = subprocess.run(
        ["crane", "config", f"{repository}:{tag}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return None
    try:
        config = json.loads(probe.stdout)
    except json.JSONDecodeError:
        return None
    labels = (config.get("config") or {}).get("Labels") or {}
    return labels or None


def base_digest(base: str) -> str | None:
    """The manifest-LIST digest a base tag resolves to, registry-side, no pull."""
    probe = subprocess.run(["crane", "digest", base], capture_output=True, text=True, check=False)
    return probe.stdout.strip() or None


def build_matrix(repository: str, *, force: bool, only: str | None) -> list[dict]:
    lines = latest_patch_per_line()
    if len(lines) < MIN_LINES:
        raise SystemExit(
            f"only {len(lines)} ansible-core line(s) at or above {FLOOR[0]}.{FLOOR[1]} -- "
            "PyPI's shape has probably changed. Refusing to publish a truncated matrix."
        )

    entries: list[dict] = []
    for line in sorted(lines):
        name = f"{line[0]}.{line[1]}"
        if only and only != name:
            continue
        version = lines[line]
        pythons = declared_pythons(version)
        if not pythons:
            raise SystemExit(
                f"ansible-core {version} declares no python classifiers, so its base "
                "cannot be derived. Refusing to guess."
            )
        newest = pythons[-1]
        base = f"python:{newest[0]}.{newest[1]}-slim"
        digest = base_digest(base)
        if digest is None:
            raise SystemExit(
                f"ansible-core {version} wants {base}, which does not resolve. A new line "
                "declaring a python we have no base image for is a stop, never a silent "
                "fall back to an older interpreter -- that would contradict the rule the "
                "whole matrix is built on."
            )

        entry = {
            "line": name,
            "version": version,
            "python": f"{newest[0]}.{newest[1]}",
            "base": base,
            "base_digest": digest,
            "declared": [f"{a}.{b}" for a, b in pythons],
        }

        if force:
            entry["reason"] = "forced"
            entries.append(entry)
            continue

        provenance = published_provenance(repository, name)
        if provenance is None:
            entry["reason"] = "not published, or no provenance"
        elif provenance.get("io.terrapod.ee.ansible-core") != version:
            entry["reason"] = (
                f"ansible-core moved: published "
                f"{provenance.get('io.terrapod.ee.ansible-core')} -> {version}"
            )
        elif provenance.get("io.terrapod.ee.base-digest") != digest:
            entry["reason"] = "base image digest moved"
        else:
            continue
        entries.append(entry)

    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="the EE image repository")
    parser.add_argument("--force", action="store_true", help="rebuild regardless of provenance")
    parser.add_argument("--line", default=None, help="restrict to one line, e.g. 2.16")
    parser.add_argument("--all", action="store_true", help="print the whole matrix, not the delta")
    arguments = parser.parse_args()

    try:
        entries = build_matrix(
            arguments.repository, force=arguments.force or arguments.all, only=arguments.line
        )
    except urllib.error.URLError as error:
        print(f"could not reach PyPI: {error}", file=sys.stderr)
        return 1

    # TWO shapes, because a GitHub matrix `include` EXTENDS combinations rather
    # than crossing with another dimension -- pairing `include` with
    # `arch: [amd64, arm64]` yields N+2 entries, not N*2. So the cross product is
    # built here, where it can be asserted, and each entry is self-contained.
    build = [entry | {"arch": arch} for entry in entries for arch in ARCHES]
    print(json.dumps({"build": build, "lines": entries}))
    for entry in entries:
        print(
            f"  {entry['line']:5} -> ansible-core {entry['version']:9} on {entry['base']:20} "
            f"({entry.get('reason', 'build')})",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
