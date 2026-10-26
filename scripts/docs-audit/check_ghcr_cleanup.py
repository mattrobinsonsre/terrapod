#!/usr/bin/env python3
"""Every container package this repository publishes is reaped by `cleanup.yml`.

Twice now a published package has been left out of that workflow and its
versions accumulated with nothing to remove them. The first time it was
`terrapod-listener` and `terrapod-migrations`, and the comment recording that is
still in the file. The second time was everything else: measured 2026-10-26, the
Helm chart package held **408** versions, `mirror/python` **330**,
`mirror/postgres` 71 and `terrapod-ee` 54, none of them reaped by anything.

A package missing from that matrix fails silently and invisibly -- nothing turns
red, the versions simply pile up -- which is exactly the shape a gate is for.

## What is derived and what is listed, and why the split

The **mirror** packages are derived from `.github/mirror-images.txt`, applying
the same flattening `mirror-images.yml` applies, because that list is the part
that moves: somebody adds a line to it and a new package appears. Deriving it
means the gate cannot go stale against the thing most likely to change.

Everything else is **listed here with its reason**, because those names live in
three different shapes across three workflows -- a matrix in `ci.yml`, an
`EE_REPOSITORY` env in `ee-build.yml`, a `helm push` at release -- and a regex
over all of them would be a derivation that looks authoritative while being
unable to see a fourth shape. An honest ledger that fails when it disagrees with
the matrix is worth more than a clever matcher with a blind spot.

The ledger is checked in BOTH directions: a published package missing from the
matrix fails, and a matrix entry naming nothing we publish fails too, because a
typo there reaps nothing while looking like coverage.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
CLEANUP = ROOT / ".github" / "workflows" / "cleanup.yml"
MIRROR_LIST = ROOT / ".github" / "mirror-images.txt"

#: Packages published by something other than the mirror workflow, each with the
#: workflow that publishes it. Keep the reason with the entry: the next person
#: deciding whether a package may be reaped needs to know what the tags MEAN.
PUBLISHED: dict[str, str] = {
    # ci.yml's build-images matrix, tagged `sha-<short>` per commit and `vX.Y.Z`
    # at release.
    "terrapod-api": "ci.yml",
    "terrapod-web": "ci.yml",
    "terrapod-runner": "ci.yml",
    "terrapod-listener": "ci.yml",
    "terrapod-migrations": "ci.yml",
    # The Helm chart, pushed as an OCI artifact at release. Versions are
    # `1.11.1`-shaped with NO `v` prefix, which is what broke the old keep-rule.
    "terrapod": "ci.yml (helm push at release)",
    # The Ansible execution environments. **Every tag is a choice offered to an
    # operator and no line is deprecated** -- a workspace pins one in
    # `ansible_version` -- so a reaped tag breaks every workspace on that line.
    # Safe to reap only the untagged leftovers of its weekly rebuilds.
    "terrapod-ee": "ee-build.yml",
    # The test base image (#2248). Tagged `sha-<input hash>` so the reap rule
    # takes it by shape, and harmlessly: `ensure_test_image` rebuilds it.
    "terrapod-test-base": "ci.yml (build-test-base)",
}

#: A floor, so a matcher that stops matching fails here instead of passing on an
#: empty set. Chosen below the real count rather than equal to it, because this
#: is a guard against the regex breaking, not a second copy of the ledger.
MIN_PACKAGES = 12


def mirror_packages() -> set[str]:
    """`mirror/<repo with slashes flattened>` for every line in the list.

    Mirrors `mirror-images.yml`'s own `flat="${repo//\\//-}"`, taken from the
    repository half of each `repo:tag` line. Comments and blanks are skipped the
    way the workflow's `[ -n "$src" ] || continue` skips them.
    """
    out: set[str] = set()
    for raw in MIRROR_LIST.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        repo = line.rsplit(":", 1)[0]
        out.add("mirror/" + repo.replace("/", "-"))
    return out


def matrix_images() -> list[str]:
    """The `image:` list of `cleanup.yml`'s matrix, in file order."""
    text = CLEANUP.read_text()
    block = re.search(r"\n        image:\n((?:\s*(?:#.*|-\s*\S+)\n)+)", text)
    if block is None:
        sys.exit("FAIL: could not find the `image:` matrix list in cleanup.yml")
    return re.findall(r"^\s*-\s*(\S+)\s*$", block.group(1), re.MULTILINE)


def main() -> int:
    listed = matrix_images()
    duplicates = {i for i in listed if listed.count(i) > 1}
    covered = set(listed)
    expected = set(PUBLISHED) | mirror_packages()

    problems: list[str] = []

    if len(covered) < MIN_PACKAGES:
        problems.append(
            f"only {len(covered)} packages parsed out of cleanup.yml's matrix, "
            f"below the floor of {MIN_PACKAGES} — the matcher has probably broken "
            f"rather than the matrix having shrunk"
        )

    if duplicates:
        problems.append(f"listed twice in the cleanup matrix: {sorted(duplicates)}")

    for missing in sorted(expected - covered):
        why = PUBLISHED.get(missing, "mirror-images.yml")
        problems.append(
            f"{missing} is published by {why} and is NOT in cleanup.yml's matrix, "
            f"so its versions accumulate with nothing to remove them"
        )

    for extra in sorted(covered - expected):
        problems.append(
            f"{extra} is in cleanup.yml's matrix but nothing here publishes it — "
            f"a name that matches no package reaps nothing while looking like "
            f"coverage, so either fix the spelling or record it in PUBLISHED"
        )

    if problems:
        print("FAIL: GHCR cleanup coverage")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"OK: cleanup.yml reaps all {len(covered)} published packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
