#!/usr/bin/env python3
"""A `docker pull` of an ARCH-TAGGED image must name the platform.

Both `Scan images` jobs run on an amd64 runner; only the image TAG is per
architecture. So `docker pull .../terrapod-api:sha-<short>-arm64` without
`--platform` asks for the runner's own platform, the arm64 tag's manifest list
has no amd64 entry, and the pull fails:

    no matching manifest for linux/amd64 in the manifest list entries

That shipped and stayed red on `main` for several pushes. It survived review
because the branch is **push-only** -- on a pull request the images arrive as
build artifacts and are `docker load`ed, never pulled -- so the half exercised
while the change was under review was the half that could not fail.

The rule, kept deliberately narrow so it has no false positives: if a
`docker pull`'s image reference interpolates a variable whose name contains
ARCH, the same command must carry `--platform`. A pull of an untagged or
single-platform image is not matched and is not this gate's business.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SELF = pathlib.Path(__file__).resolve()

# `docker pull` ... through to end of line. Captures the whole command so the
# platform flag can be looked for anywhere in it, not just immediately after.
PULL = re.compile(r"docker\s+pull\b[^\n]*")
# An arch-bearing interpolation: ${ARCH}, $ARCH, ${matrix.arch}, ${TARGETARCH}.
ARCH_REF = re.compile(r"\$\{?[A-Za-z0-9_.]*ARCH[A-Za-z0-9_.]*\}?", re.IGNORECASE)

SEARCH = [".github/workflows", "scripts", "docker", "Makefile", "Tiltfile"]
MIN_PULLS = 1  # a floor: if nothing matches, the matcher has stopped matching


def main() -> int:
    files: list[pathlib.Path] = []
    for rel in SEARCH:
        p = ROOT / rel
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(q for q in p.rglob("*") if q.is_file())

    problems: list[str] = []
    seen = 0
    for f in sorted(files):
        if f.resolve() == SELF:
            continue  # this file quotes the bad form in order to forbid it
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.split("\n"), 1):
            for cmd in PULL.findall(line):
                if not ARCH_REF.search(cmd):
                    continue
                seen += 1
                if "--platform" not in cmd:
                    problems.append(
                        f"{f.relative_to(ROOT)}:{n}: `docker pull` of an arch-tagged "
                        f"image with no --platform.\n    {cmd.strip()[:120]}\n"
                        f"    Both arch jobs run on an amd64 runner, so without it this "
                        f"asks for linux/amd64 and the arm64 manifest list has no such entry."
                    )

    if seen < MIN_PULLS:
        print(
            f"FAIL — examined {seen} arch-tagged docker pulls, expected at least "
            f"{MIN_PULLS}. The matcher has stopped matching, so this gate is "
            f"passing on nothing.",
            file=sys.stderr,
        )
        return 1

    if problems:
        for p in problems:
            print(f"FAIL — {p}", file=sys.stderr)
        return 1

    print(f"PASS — {seen} arch-tagged docker pull(s), every one naming its platform.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
